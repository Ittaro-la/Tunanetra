"""Hub event ESP32 (pemicu manual) dan sesi perangkat (tanpa AI, tanpa jaringan)."""
import pytest
from flask import Flask

from config.settings import settings
from devices.events import ESP32EventHub, normalize_event, register_esp32_events
from services.sessions import SessionManager


def make_hub(**kwargs):
    calls = []

    def trigger(**kw):
        calls.append(kw)
        return {"status": "started", "request_id": f"r{len(calls)}"}
    kwargs.setdefault("cooldown_ms", 0)
    kwargs.setdefault("trigger", trigger)
    return ESP32EventHub(SessionManager(), **kwargs), calls


@pytest.mark.parametrize("event,mode", [("object_detection", "objek"), ("sign_detection", "rambu")])
def test_each_event_triggers_its_mode_on_a_device_session(event, mode):
    hub, calls = make_hub()
    monitor = hub.subscribe("monitor")
    outcome = hub.dispatch(event=event, device_id="device-a", source="websocket")
    assert outcome["delivered"] == 1 and outcome["status"] == "started"
    assert calls[-1]["mode"] == mode and calls[-1]["session_id"] == "dev-device-a"
    assert monitor.queue.get_nowait()["type"] == "quick_started"


def test_event_names_and_aliases_map_to_modes():
    assert normalize_event("object_detection")[1] == "objek"
    assert normalize_event("Sign-Detection") == ("sign_detection", "rambu")
    assert normalize_event("rambu")[1] == "rambu"
    # deteksi ekspresi dan teks sudah dihapus: tidak dikenali lagi
    for gone in ("expression_detection", "text_detection", "ocr", "hack"):
        with pytest.raises(ValueError):
            normalize_event(gone)
    with pytest.raises(ValueError):
        normalize_event()


def test_cooldown_ignores_duplicate_event_but_not_other_events():
    hub, calls = make_hub(cooldown_ms=60000)
    assert hub.dispatch(event="object_detection", device_id="d")["delivered"] == 1
    again = hub.dispatch(event="object_detection", device_id="d")
    assert again["status"] == "ignored" and again["delivered"] == 0
    assert hub.dispatch(event="sign_detection", device_id="d")["delivered"] == 1
    assert len(calls) == 2


def test_rejected_trigger_is_reported_and_not_delivered():
    hub, _ = make_hub(trigger=lambda **kw: {"status": "busy", "reason": "sibuk"})
    monitor = hub.subscribe("m")
    outcome = hub.dispatch(event="object_detection", device_id="d")
    assert outcome["status"] == "busy" and outcome["delivered"] == 0
    assert monitor.queue.get_nowait()["type"] == "quick_rejected"


def test_missing_or_crashing_trigger_never_raises():
    hub, _ = make_hub(trigger=None)
    assert hub.dispatch(event="object_detection", device_id="d")["status"] == "no_trigger"
    hub2, _ = make_hub(trigger=lambda **kw: 1 / 0)
    assert hub2.dispatch(event="object_detection", device_id="d")["status"] == "error"


def test_existing_pairing_is_kept_over_the_default_session():
    hub, calls = make_hub()
    hub._sessions.create_session("phone")
    hub._sessions.link_device("d", "phone")
    hub.dispatch(event="object_detection", device_id="d")
    assert calls[-1]["session_id"] == "phone"


def test_slow_monitor_queue_drops_oldest_and_never_blocks():
    hub, _ = make_hub()
    monitor = hub.subscribe("slow")
    for _ in range(100):
        hub.dispatch(event="object_detection", device_id="d")
    assert monitor.queue.qsize() == 32


def test_rest_endpoint_requires_token_when_configured():
    app = Flask(__name__)
    hub = register_esp32_events(app, SessionManager(), settings=type("S", (), {
        "esp32_event_cooldown_ms": 0, "esp32_shared_token": "rahasia",
        "esp32_max_stream_clients": 4, "esp32_sse_heartbeat_seconds": 15})())
    hub.trigger = lambda **kw: {"status": "started", "request_id": "x"}
    client = app.test_client()
    assert client.post("/api/esp32/event", json={"event": "object_detection"}).status_code == 401
    ok = client.post("/api/esp32/event", json={"event": "object_detection", "token": "rahasia"})
    assert ok.status_code == 200 and ok.json["status"] == "started"
    assert client.post("/api/esp32/event", json={"event": "ocr", "token": "rahasia"}).status_code == 400
    assert client.get("/api/esp32/status").json["trigger_ready"] is True


def test_ensure_device_session_is_stable_and_sanitised():
    sessions = SessionManager()
    first = sessions.ensure_device_session("esp32/A 1")
    assert first == "dev-esp32_A_1" and sessions.ensure_device_session("esp32/A 1") == first
    assert sessions.device_for_session(first) == "esp32/A 1"
    assert sessions.ensure_device_session("") == "dev-unknown"


def test_device_mode_defaults_to_normal_and_rejects_garbage():
    sessions = SessionManager()
    assert sessions.get_device_mode("d") == "normal"
    assert sessions.set_device_mode("d", "hemat") == "hemat"
    assert sessions.get_device_mode("d") == "hemat"
    assert sessions.set_device_mode("d", "turbo") is None       # tidak valid: mode lama tetap
    assert sessions.get_device_mode("d") == "hemat"
    assert sessions.set_device_mode("d", " NORMAL ") == "normal"
