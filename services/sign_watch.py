"""Deteksi rambu yang berjalan terus-menerus (mode biasa).

``SignWatcher`` adalah thread latar: tiap ``SIGN_WATCH_INTERVAL_SECONDS`` ia
memotret satu frame dari ESP32-CAM, menjalankan YOLO rambu, lalu mengucapkan
hasilnya di TWS lewat ESP32, mis. "Dilarang Parkir berada pada arah jam 11,
sekitar 2 meter." Arah jam dan jarak dihitung di ``ai/sign_geometry.py``.

Supaya tidak cerewet, ``SignAnnouncer`` hanya mengumumkan rambu yang:

* terlihat di ``SIGN_CONFIRM_FRAMES`` siklus berturut-turut (menekan deteksi palsu),
* belum diumumkan dalam ``SIGN_ANNOUNCE_COOLDOWN_SECONDS``, atau arah jamnya
  berubah jauh (>= 2 jam) sejak diumumkan.

Watcher HANYA bekerja bila ada ESP32 terhubung dengan mode ``normal``. Di mode
hemat daya (atau tanpa perangkat) ia diam dan tidak memotret sama sekali.
"""
from __future__ import annotations

import logging
import threading
import time

from ai.sign_geometry import circular_hour_gap, sentence
from camera import CameraError
from services.quick_commands import CAMERA_DOWN_SPOKEN

SIGN_UNAVAILABLE_SPOKEN = "Deteksi rambu belum tersedia."
CAMERA_FAILURES_BEFORE_ANNOUNCE = 5


class SignAnnouncer:
    """Mesin keputusan murni (tanpa I/O): rambu mana yang layak diucapkan sekarang."""

    def __init__(self, *, confirm_frames=2, cooldown=20.0, min_confidence=0.5,
                 reannounce_gap=5.0, hour_change=2, max_items=2):
        self.confirm_frames = max(1, int(confirm_frames))
        self.cooldown = max(0.0, float(cooldown))
        self.min_confidence = float(min_confidence)
        self.reannounce_gap = max(0.0, float(reannounce_gap))
        self.hour_change = max(1, int(hour_change))
        self.max_items = max(1, int(max_items))
        self._streak = {}     # label -> siklus berturut-turut terlihat
        self._last = {}       # label -> (waktu diumumkan, jam saat itu)

    def reset(self):
        self._streak.clear()
        self._last.clear()

    def update(self, items, now):
        """``items``: deteksi terurut dari yang terdekat. Mengembalikan yang diumumkan sekarang."""
        seen = {}
        for item in items or []:
            if float(item.get("confidence", 0.0)) < self.min_confidence:
                continue
            seen.setdefault(str(item["label"]), item)     # terdekat per label
        for label in [k for k in self._streak if k not in seen]:
            del self._streak[label]                       # hilang dari pandangan: ulang hitungan
        for label in seen:
            self._streak[label] = self._streak.get(label, 0) + 1

        chosen = []
        for label, item in seen.items():
            if self._streak[label] < self.confirm_frames:
                continue
            last = self._last.get(label)
            if last is None or now - last[0] >= self.cooldown:
                chosen.append(item)
                continue
            gap = circular_hour_gap(last[1], item.get("clock_hour"))
            if gap is not None and gap >= self.hour_change and now - last[0] >= self.reannounce_gap:
                chosen.append(item)
        chosen = chosen[:self.max_items]
        for item in chosen:
            self._last[str(item["label"])] = (now, item.get("clock_hour"))
        return chosen


class SignWatcher:
    def __init__(self, quick, sessions, settings, logger=None, *, clock=time.monotonic):
        self.quick = quick
        self.sessions = sessions
        self.settings = settings
        self.log = logger or logging.getLogger(__name__)
        self.clock = clock
        self.interval = max(0.2, float(settings.sign_watch_interval_seconds))
        self.speak_distance = bool(settings.sign_speak_distance)
        self.announcer = SignAnnouncer(
            confirm_frames=settings.sign_confirm_frames,
            cooldown=settings.sign_announce_cooldown_seconds)
        self._camera_failures = 0
        self._camera_down_spoken = False
        self._model_down_spoken = False
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if not self.settings.sign_watch_enabled:
            self.log.info("[sign-watch] nonaktif (SIGN_WATCH_ENABLED=false)")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sign-watch", daemon=True)
        self._thread.start()
        self.log.info("[sign-watch] deteksi rambu terus-menerus aktif (tiap %.1f dtk, mode biasa saja)",
                      self.interval)
        return True

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                delay = self.step()
            except Exception:                              # pragma: no cover - jaring pengaman
                self.log.exception("[sign-watch] galat tak terduga")
                delay = 5.0
            self._stop.wait(delay)

    # ------------------------------------------------------------------ step
    def _target_device(self):
        for device_id in self.sessions.online_devices():
            if self.sessions.get_device_mode(device_id) == "normal":
                return device_id
        return None

    def step(self):
        """Satu siklus. Mengembalikan jeda (detik) sebelum siklus berikutnya."""
        device = self._target_device()
        if device is None:
            # Mode hemat daya / tidak ada perangkat: tidak memotret, tidak mendeteksi.
            self.announcer.reset()
            self._camera_failures = 0
            return 1.0
        sid = self.sessions.ensure_device_session(device)

        try:
            result = self.quick.detect_signs_once()
        except CameraError as exc:
            self._camera_failures += 1
            self.log.warning("[sign-watch] kamera gagal (%d): %s", self._camera_failures, exc)
            if self._camera_failures >= CAMERA_FAILURES_BEFORE_ANNOUNCE and not self._camera_down_spoken:
                self._camera_down_spoken = True           # sekali per gangguan, bukan tiap siklus
                self.quick.speak(sid, CAMERA_DOWN_SPOKEN)
            return min(10.0, self.interval * (self._camera_failures + 1))
        except Exception as exc:
            self.log.warning("[sign-watch] deteksi rambu gagal: %s", exc)
            if not self._model_down_spoken:
                self._model_down_spoken = True
                self.quick.speak(sid, SIGN_UNAVAILABLE_SPOKEN)
            return 10.0

        self._camera_failures = 0
        self._camera_down_spoken = False
        self._model_down_spoken = False
        if result is None:                                 # AI dipakai deteksi objek
            return self.interval

        chosen = self.announcer.update(result.get("sign_items", []), self.clock())
        if chosen:
            text = " ".join(sentence(item, self.speak_distance) for item in chosen)
            self.log.info("[sign-watch] umumkan: %s", text)
            self.quick.speak(sid, text)
        return self.interval
