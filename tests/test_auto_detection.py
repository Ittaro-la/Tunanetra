"""Deteksi rambu terus-menerus (arah jam + jarak), pengumuman, dan SignWatcher. Tanpa AI/jaringan."""
from unittest.mock import Mock

import pytest

from ai.sign_geometry import SignGeometry, circular_hour_gap, sentence
from camera import CameraError
from services.sessions import SessionManager
from services.sign_watch import SignAnnouncer, SignWatcher

W, H = 640, 480


def hour_for(cx, cy, mirrored=False):
    return SignGeometry(mirrored=mirrored).clock_hour([cx - 20, cy - 20, 40, 40], W, H)


@pytest.mark.parametrize("cx,cy,expected", [(320, 40, 12), (600, 240, 3), (320, 450, 6), (40, 240, 9),
                                            (120, 60, 10), (500, 60, 2)])
def test_clock_hour_from_camera_center_to_box_center(cx, cy, expected):
    assert hour_for(cx, cy) == expected


def test_box_on_the_camera_center_is_straight_ahead_and_mirror_flips_left_right():
    assert hour_for(322, 242) is None
    assert hour_for(600, 240, mirrored=True) == 9           # camera.mirror(1): kanan gambar = kiri nyata


def test_distance_follows_reference_pixels_and_frame_width():
    g = SignGeometry(ref_pixels=85, ref_distance_m=2.0, ref_frame_width=640)
    assert g.distance_m([0, 0, 85, 85], 640) == 2.0
    assert g.distance_m([0, 0, 170, 60], 640) == 1.0         # sisi terpanjang dipakai
    assert g.distance_m([0, 0, 42.5, 42.5], 640) == 4.0
    assert g.distance_m([0, 0, 85, 85], 1280) == 4.0         # piksel dinormalkan ke resolusi kalibrasi
    assert g.distance_m([0, 0, 0, 0], 640) is None


def test_sentence_names_sign_clock_and_distance():
    g = SignGeometry(mirrored=False)
    item = g.describe({"label": "Dilarang Parkir", "confidence": .9, "box": [100, 60, 85, 85]}, W, H)
    assert sentence(item) == "Dilarang Parkir berada pada arah jam 10, sekitar 2 meter."
    assert sentence(item, speak_distance=False) == "Dilarang Parkir berada pada arah jam 10."
    zebra = g.describe({"label": "Zebra Cross", "confidence": .6, "box": [0, 300, 640, 150]}, W, H)
    assert zebra["distance_m"] is None and "sekitar" not in sentence(zebra)


def test_circular_gap():
    assert circular_hour_gap(11, 1) == 2 and circular_hour_gap(12, 6) == 6 and circular_hour_gap(None, 3) is None


def item(label, hour, confidence=.8):
    return {"label": label, "clock_hour": hour, "confidence": confidence, "distance_m": 2.0}


def test_announcer_needs_confirmation_then_respects_cooldown():
    a = SignAnnouncer(confirm_frames=2, cooldown=20)
    assert a.update([item("Stop", 11)], 0) == []              # baru sekali terlihat
    assert [i["label"] for i in a.update([item("Stop", 11)], 1)] == ["Stop"]
    assert a.update([item("Stop", 11)], 2) == []              # masih jeda
    assert [i["label"] for i in a.update([item("Stop", 2)], 8)] == ["Stop"]   # arah jam berubah >= 2
    assert a.update([item("Stop", 2)], 9) == []
    assert [i["label"] for i in a.update([item("Stop", 2)], 30)] == ["Stop"]  # jeda lewat


def test_announcer_resets_streak_when_sign_disappears_and_ignores_weak_detections():
    a = SignAnnouncer(confirm_frames=2, cooldown=20)
    a.update([item("Stop", 3)], 0)
    a.update([], 1)
    assert a.update([item("Stop", 3)], 2) == []               # hitungan mulai dari awal
    assert a.update([item("Lemah", 3, .2)], 3) == [] and a.update([item("Lemah", 3, .2)], 4) == []


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


def make_watcher(detect, mode="normal", online=True):
    sessions = SessionManager()
    if online:
        sessions.register_esp32_socket("esp-1", object())
        sessions.set_device_mode("esp-1", mode)
    quick = Mock(detect_signs_once=Mock(side_effect=detect), speak=Mock(return_value=True))
    settings = type("S", (), {"sign_watch_enabled": True, "sign_watch_interval_seconds": 1.0,
                              "sign_speak_distance": True, "sign_confirm_frames": 2,
                              "sign_announce_cooldown_seconds": 20.0})()
    clock = Clock()
    return SignWatcher(quick, sessions, settings, clock=clock), quick, clock


def frame_result():
    g = SignGeometry(mirrored=False)
    return {"sign_items": [g.describe({"label": "Dilarang Parkir", "confidence": .9,
                                       "box": [100, 60, 85, 85]}, W, H)]}


def test_watcher_speaks_sign_with_clock_direction_after_confirmation():
    watcher, quick, clock = make_watcher(lambda: frame_result())
    watcher.step()
    quick.speak.assert_not_called()
    clock.now = 1
    watcher.step()
    quick.speak.assert_called_once_with("dev-esp-1", "Dilarang Parkir berada pada arah jam 10, sekitar 2 meter.")
    clock.now = 2
    watcher.step()
    quick.speak.assert_called_once()                          # tidak cerewet: sama, masih dalam jeda


def test_watcher_does_nothing_in_power_saving_mode_or_without_device():
    for mode, online in (("hemat", True), ("normal", False)):
        watcher, quick, _ = make_watcher(lambda: frame_result(), mode=mode, online=online)
        assert watcher.step() == 1.0
        quick.detect_signs_once.assert_not_called()           # kamera tidak dipakai sama sekali


def test_watcher_skips_cycle_when_ai_busy_and_announces_camera_outage_once():
    watcher, quick, _ = make_watcher(lambda: None)
    assert watcher.step() == 1.0 and not quick.speak.called
    quick.detect_signs_once.side_effect = CameraError("mati")
    for _ in range(8):
        watcher.step()
    assert quick.speak.call_count == 1 and "Kamera tidak terhubung" in quick.speak.call_args.args[1]
    quick.detect_signs_once.side_effect = lambda: frame_result()
    watcher.step()
    quick.detect_signs_once.side_effect = CameraError("mati lagi")
    for _ in range(6):
        watcher.step()
    assert quick.speak.call_count == 2                        # gangguan baru diumumkan lagi


def test_watcher_model_failure_is_spoken_once_and_backs_off():
    watcher, quick, _ = make_watcher(lambda: (_ for _ in ()).throw(RuntimeError("model hilang")))
    assert watcher.step() == 10.0 and watcher.step() == 10.0
    quick.speak.assert_called_once_with("dev-esp-1", "Deteksi rambu belum tersedia.")
