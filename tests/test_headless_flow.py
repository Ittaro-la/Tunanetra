"""Alur mandiri: ESP32 -> ESP32-CAM -> AI -> TTS -> suara dikirim ke ESP32 (tanpa browser).

Audio dikirim lewat HTTP PCM (``push_mp3_to_esp32``), bukan lewat pesan WebSocket
``tts_ready``; di test fungsi itu diganti Mock.
"""
import io
import json
import threading
import time
from unittest.mock import Mock

import numpy as np
import pytest

from camera import CameraError
from server.app import create_app


class FakeDeviceSocket:
    """Menggantikan koneksi WebSocket ESP32: menampung semua pesan dari server."""

    def __init__(self):
        self.sent = []

    def send_json(self, payload):
        self.sent.append(payload)
        return True

    def of_type(self, kind):
        return [m for m in self.sent if m.get("type") == kind]


class FakeWS:
    """Objek ws untuk DeviceLink._endpoint (receive/send seperti simple_websocket)."""

    def __init__(self, messages):
        self.inbox = [json.dumps(m) for m in messages]
        self.outbox = []

    def receive(self):
        return self.inbox.pop(0) if self.inbox else None

    def send(self, data):
        self.outbox.append(json.loads(data))


def wait_for(predicate, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def box():
    return {"label": "chair", "confidence": .8, "box": [200, 200, 200, 200], "area_ratio": .13}


@pytest.fixture
def system(tmp_path, monkeypatch, frame):
    monkeypatch.setattr("server.app.BASE_DIR", tmp_path)
    monkeypatch.setattr("services.quick_commands.BASE_DIR", tmp_path)
    pushed = Mock(return_value=True)
    monkeypatch.setattr("services.quick_commands.push_mp3_to_esp32", pushed)
    monkeypatch.setattr("server.upload.push_mp3_to_esp32", pushed)
    vision = {
        "object": Mock(detect_boxes=Mock(return_value=[box()]), diagnostics=Mock(return_value={}),
                       summarize=Mock(return_value=["kursi"]), instance=None, error=None),
        "sign": Mock(analyze=Mock(return_value={"text": "", "confidence": 0.0, "reliable": False, "boxes": []})),
        "scene": Mock(detect_boxes=Mock(return_value=[])),
    }
    audio = Mock(generate_tts=Mock(side_effect=lambda text: io.BytesIO(b"mp3:" + text.encode())),
                 _play_audio=Mock(return_value=False), stt_available=False)
    camera = Mock(capture_frames=Mock(side_effect=lambda n: [frame.copy() for _ in range(n)]),
                  capture=Mock(return_value=frame.copy()))
    database = Mock()
    app, ctx = create_app(camera=camera, audio=audio, groq=None, database=database, vision=vision)
    ctx.hub.cooldown_ms = 0
    device = FakeDeviceSocket()
    ctx.sessions.register_esp32_socket("esp-1", device)
    ctx.sessions.set_device_ip("esp-1", "192.168.1.50")
    ctx.sessions.ensure_device_session("esp-1")
    return app, ctx, device, camera, vision, audio, database, pushed


def test_manual_object_event_runs_detection_and_speaks_on_esp32(system):
    app, ctx, device, camera, vision, audio, database, pushed = system
    outcome = ctx.hub.dispatch(event="object_detection", device_id="esp-1", source="websocket")
    assert outcome["status"] == "started" and outcome["delivered"] == 1
    assert wait_for(lambda: pushed.called)
    camera.capture_frames.assert_called_once_with(3)              # ESP32-CAM, bukan browser
    assert vision["object"].detect_boxes.call_count == 3
    assert pushed.call_args.args[1] == "192.168.1.50"             # suara dikirim ke IP ESP32
    assert wait_for(lambda: database.save_interaction.called)


def test_sign_event_runs_only_the_sign_detector(system):
    app, ctx, device, camera, vision, audio, database, pushed = system
    ctx.hub.dispatch(event="sign_detection", device_id="esp-1", source="websocket")
    assert wait_for(lambda: pushed.called)
    camera.capture_frames.assert_called_once_with(1)
    assert vision["sign"].mock_calls
    assert not vision["object"].mock_calls


def test_second_trigger_while_busy_is_rejected_not_queued(system):
    app, ctx, device, camera, vision, audio, database, pushed = system
    entered, release = threading.Event(), threading.Event()

    def slow(count):
        entered.set()
        assert release.wait(4)
        return [np.full((480, 640, 3), 128, np.uint8) for _ in range(count)]
    camera.capture_frames.side_effect = slow
    assert ctx.hub.dispatch(event="object_detection", device_id="esp-1")["delivered"] == 1
    assert entered.wait(2)
    busy = ctx.hub.dispatch(event="sign_detection", device_id="esp-1")
    assert busy["status"] == "busy" and busy["delivered"] == 0
    release.set()
    assert wait_for(lambda: pushed.called)
    assert wait_for(lambda: ctx.hub.dispatch(event="sign_detection", device_id="esp-1")["delivered"] == 1)


def test_camera_failure_is_announced_by_voice(system):
    app, ctx, device, camera, vision, audio, database, pushed = system
    camera.capture_frames.side_effect = CameraError("ESP32-CAM tidak dapat dihubungi")
    ctx.hub.dispatch(event="object_detection", device_id="esp-1")
    assert wait_for(lambda: pushed.called)
    assert "Kamera tidak terhubung" in audio.generate_tts.call_args.args[0]
    assert not vision["object"].detect_boxes.called
    camera.capture_frames.side_effect = None                      # kegagalan tidak mengunci perintah berikutnya
    camera.capture_frames.return_value = [np.full((480, 640, 3), 128, np.uint8)] * 3
    assert wait_for(lambda: ctx.hub.dispatch(event="object_detection", device_id="esp-1")["delivered"] == 1)


def test_failure_falls_back_to_beep_when_tts_is_down(system):
    app, ctx, device, camera, vision, audio, database, pushed = system
    audio.generate_tts.side_effect = RuntimeError("offline")
    camera.capture_frames.side_effect = CameraError("mati")
    ctx.hub.dispatch(event="object_detection", device_id="esp-1")
    assert wait_for(lambda: device.of_type("error"))


def test_without_esp32_socket_audio_plays_locally(system):
    app, ctx, device, camera, vision, audio, *_ = system
    ctx.sessions.unregister_esp32_socket("esp-1")
    ctx.hub.dispatch(event="object_detection", device_id="esp-1")
    assert wait_for(lambda: audio._play_audio.called)


def test_websocket_register_reports_mode_and_mode_change_is_stored(system):
    app, ctx, *_ = system
    ctx.sessions.unregister_esp32_socket("esp-1")
    ws = FakeWS([{"type": "register", "role": "esp32", "device_id": "esp-1", "ip": "1.2.3.4", "mode": "hemat"},
                 {"type": "mode_change", "mode": "normal"}])
    ctx.link._endpoint(ws)
    assert [m["type"] for m in ws.outbox] == ["registered"]
    assert ws.outbox[0]["paired_session"] == "dev-esp-1"
    assert ctx.sessions.get_device_mode("esp-1") == "normal"
    assert ctx.sessions.get_device_ip("esp-1") == "1.2.3.4"


def test_invalid_mode_change_gets_error_and_keeps_old_mode(system):
    app, ctx, *_ = system
    ws = FakeWS([{"type": "register", "role": "esp32", "device_id": "esp-1"},
                 {"type": "mode_change", "mode": "turbo"}])
    ctx.link._endpoint(ws)
    assert ws.outbox[-1]["type"] == "error"
    assert ctx.sessions.get_device_mode("esp-1") == "normal"


def test_obstacle_sample_within_range_starts_object_detection_without_reply(system):
    app, ctx, device, camera, vision, audio, database, pushed = system
    ws = FakeWS([{"type": "register", "role": "esp32", "device_id": "esp-1"},
                 {"type": "quick_command", "event": "obstacle_warning", "distance_cm": 95, "device_id": "esp-1"}])
    ctx.link._endpoint(ws)
    assert [m["type"] for m in ws.outbox] == ["registered"]        # tanpa ack -> tanpa beep tiap sampel
    assert ctx.sessions.get("dev-esp-1")["distance_samples"][-1][1] == 95
    # FakeWS menutup koneksi di akhir, jadi suara jatuh ke speaker lokal; yang diuji: kamera jalan.
    assert wait_for(lambda: camera.capture_frames.called)
    camera.capture_frames.assert_called_once_with(3)


def test_obstacle_samples_respect_cooldown_range_and_mode(system):
    app, ctx, device, camera, vision, audio, database, pushed = system
    quick = ctx.quick
    quick.settings = type("S", (), {"object_trigger_cm": 100.0, "object_auto_cooldown_seconds": 60.0})()
    sid = ctx.sessions.ensure_device_session("esp-1")

    def sample(distance):
        return quick.handle_distance(device_id="esp-1", session_id=sid, distance_cm=distance)

    assert sample(180)["status"] == "ignored"                      # terlalu jauh
    ctx.sessions.set_device_mode("esp-1", "hemat")
    assert sample(80)["status"] == "ignored"                       # mode hemat: tanpa deteksi
    assert not camera.capture_frames.called
    ctx.sessions.set_device_mode("esp-1", "normal")
    assert sample(80)["status"] == "started"
    assert wait_for(lambda: pushed.called)
    assert wait_for(lambda: quick._auto_object_next_ok > time.monotonic())   # jeda mulai setelah selesai
    again = sample(80)                                             # benda tetap di ~1 m
    assert again["status"] == "cooldown" and again["retry_in_seconds"] > 30
    assert camera.capture_frames.call_count == 1                   # kamera tidak jalan terus
    quick._auto_object_next_ok = 0.0                               # jeda habis
    assert wait_for(lambda: sample(80)["status"] == "started")


def test_recorded_sensor_distance_reaches_object_summary(system):
    app, ctx, device, camera, vision, audio, database, pushed = system
    ctx.sessions.record_distance("dev-esp-1", 40)
    ctx.hub.dispatch(event="object_detection", device_id="esp-1")
    assert wait_for(lambda: pushed.called)
    assert "sensor membaca halangan sekitar 40 cm" in audio.generate_tts.call_args.args[0]


def test_unknown_event_is_rejected_with_error(system):
    app, ctx, device, *_ = system
    ws = FakeWS([{"type": "register", "role": "esp32", "device_id": "esp-1"},
                 {"type": "quick_command", "event": "expression_detection", "device_id": "esp-1"}])
    ctx.link._endpoint(ws)
    assert ws.outbox[-1]["type"] == "error"


def test_http_event_endpoint_starts_detection(system):
    app, ctx, device, camera, vision, audio, database, pushed = system
    response = app.test_client().post("/api/esp32/event", json={"event": "sign_detection", "device_id": "esp-1"})
    assert response.status_code == 200 and response.json["status"] == "started"
    assert wait_for(lambda: pushed.called)


def test_upload_text_question_uses_esp32_cam_and_speaks_on_device(system):
    app, ctx, device, camera, vision, audio, database, pushed = system
    response = app.test_client().post("/api/upload", data={
        "device_id": "esp-1", "request_id": "q1", "transcript": "apa yang ada di depan saya"})
    assert response.status_code == 200, response.json
    assert response.json["result"] == "Saya mendeteksi kursi di depan Anda."
    assert response.json["audio_output"] == "esp32"
    camera.capture.assert_called_once()
    assert pushed.called


def test_upload_without_camera_still_answers_gracefully(system):
    app, ctx, device, camera, *_ = system
    camera.capture.return_value = None
    response = app.test_client().post("/api/upload", data={
        "device_id": "esp-1", "request_id": "q2", "transcript": "apa yang ada di depan saya"})
    assert response.status_code == 200 and "snapshot" in response.json["result"]


def test_web_ui_is_not_served_by_default(system):
    app, *_ = system
    assert app.test_client().get("/").status_code == 404
    assert app.test_client().get("/app.js").status_code == 404


def test_cooldown_starts_at_trigger_so_a_slow_detection_cannot_be_retriggered(system):
    app, ctx, device, camera, vision, audio, database, pushed = system
    quick = ctx.quick
    quick.settings = type("S", (), {"object_trigger_cm": 100.0, "object_auto_cooldown_seconds": 60.0})()
    entered, release = threading.Event(), threading.Event()

    def slow(count):
        entered.set()
        assert release.wait(4)
        return [np.full((480, 640, 3), 128, np.uint8) for _ in range(count)]
    camera.capture_frames.side_effect = slow
    sid = ctx.sessions.ensure_device_session("esp-1")
    assert quick.handle_distance(device_id="esp-1", session_id=sid, distance_cm=80)["status"] == "started"
    assert entered.wait(2)
    # Sensor masih membaca 80 cm saat kamera masih bekerja: harus "cooldown", bukan potret ulang.
    for _ in range(5):
        assert quick.handle_distance(device_id="esp-1", session_id=sid, distance_cm=80)["status"] == "cooldown"
    release.set()
    assert wait_for(lambda: pushed.called)
    assert camera.capture_frames.call_count == 1


def test_default_cooldown_is_one_minute_and_sign_watch_never_saves_images(system, monkeypatch):
    from config.settings import settings
    assert settings.object_auto_cooldown_seconds == 60.0
    app, ctx, device, camera, vision, audio, *_ = system
    saved = Mock()
    monkeypatch.setattr(ctx.quick.pipeline, "_save_annotated", saved)
    ctx.quick.pipeline.save_detection_images = True
    ctx.quick.detect_signs_once()
    saved.assert_not_called()                                  # siklus rambu berkala tidak menulis file
