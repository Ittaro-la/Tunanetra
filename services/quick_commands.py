"""Quick Command: satu tugas visi (objek / rambu) per perintah.

Pintu masuk, satu logika (``QuickCommandService.run``):

* **Sensor ultrasonik ESP32 (mode biasa)** -> ``devices.ws`` ->
  ``handle_distance()``. Benda sekitar 1 m memicu deteksi objek; setelah itu
  ada jeda 30-60 dtk (``OBJECT_AUTO_COOLDOWN_SECONDS``) sebelum bisa memicu lagi.
* **Deteksi rambu terus-menerus** -> ``services.sign_watch.SignWatcher`` ->
  ``detect_signs_once()`` + ``speak()``.
* **Uji manual** ``POST /api/esp32/event`` -> ``start_from_button()`` dan
  ``POST /api/quick`` (snapshot boleh dikirim klien; bila tidak ada, backend
  memotret lewat ESP32-CAM).

Semuanya: backend memotret lewat ESP32-CAM, menjalankan AI, lalu mengirim
suara ke speaker/TWS lewat ESP32. Tidak ada browser/webcam.

Satu pekerjaan visi aktif PER JALUR (objek dan rambu terpisah); permintaan
bersamaan pada jalur yang sama ditolak (bukan diantrekan tanpa batas). TTS dan pencatatan database berjalan di antrean
latar terbatas sehingga hasil visual tidak tertunda.
"""
import base64
import io
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
from flask import Blueprint, Response, jsonify, request, send_from_directory
from PIL import Image

from ai.pipeline import VisionPipeline
from ai.sign_geometry import SignGeometry
from camera import CameraError
from config.settings import BASE_DIR, settings as default_settings
from services.esp32_audio_push import push_mp3_to_esp32

MODE_LABEL = {
    "objek": "Deteksi Objek",
    "rambu": "Deteksi Rambu Lalu Lintas",
}
CAMERA_DOWN_SPOKEN = "Kamera tidak terhubung. Periksa daya dan Wi-Fi kamera."

_DETECTION_GALLERY_HTML = """<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<title>Spider Sense — Galeri Deteksi</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: system-ui, sans-serif; background: #111; color: #eee; margin: 0; padding: 16px; }
  h1 { font-size: 18px; margin: 0 0 12px; }
  #status { color: #999; font-size: 13px; margin-bottom: 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }
  .card { background: #1c1c1c; border-radius: 8px; overflow: hidden; }
  .card img { width: 100%; display: block; }
  .card .name { font-size: 11px; color: #aaa; padding: 6px 8px; word-break: break-all; }
  .empty { color: #888; padding: 24px 0; }
</style>
</head>
<body>
<h1>Spider Sense — Galeri Hasil Deteksi</h1>
<div id="status">Memuat...</div>
<div class="grid" id="grid"></div>
<script>
async function load() {
  const res = await fetch('/api/detection_images?limit=60');
  const data = await res.json();
  const status = document.getElementById('status');
  const grid = document.getElementById('grid');
  if (data.status === 'disabled') {
    status.textContent = data.message;
    grid.innerHTML = '';
    return;
  }
  status.textContent = data.count + ' gambar tersimpan (terbaru di atas). Auto-refresh tiap 5 detik.';
  grid.innerHTML = data.items.map(function(item) {
    return '<div class="card"><img src="' + item.url + '" loading="lazy">' +
           '<div class="name">' + item.name + '</div></div>';
  }).join('') || '<div class="empty">Belum ada gambar. Coba tombol Quick Command dulu.</div>';
}
load();
setInterval(load, 5000);
</script>
</body>
</html>"""


class QuickCommandService:
    def __init__(self, services, sessions, audio_getter, database_getter, camera=None,
                 push_device=None, notify=None, logger=None, settings=None):
        cfg = settings or default_settings
        self.settings = cfg
        self.sessions = sessions
        self.audio_getter = audio_getter
        self.database_getter = database_getter
        self.camera = camera
        self.push_device = push_device
        self.notify = notify or (lambda payload: None)
        self.log = logger or logging.getLogger(__name__)
        self.pipeline = VisionPipeline(
            services,
            sign_geometry=SignGeometry(
                ref_pixels=cfg.sign_ref_pixels, ref_distance_m=cfg.sign_ref_distance_m,
                ref_frame_width=cfg.sign_ref_frame_width, mirrored=cfg.sign_image_mirrored),
            speak_sign_distance=cfg.sign_speak_distance)
        self.services = services

        # Dua jalur visi yang TERPISAH: deteksi objek (dipicu ultrasonik) dan deteksi rambu
        # (terus-menerus) masing-masing punya kunci sendiri, jadi tidak saling menunda atau
        # saling melewati siklus. Kamera tetap satu: ESP32Cam menyerialkan pengambilan frame.
        self._vision_locks = {"objek": threading.Lock(), "rambu": threading.Lock()}
        self._button_gate = threading.Lock()
        self._jobs_lock = threading.Lock()
        # Satu suara pada satu waktu: ESP32 hanya melayani satu POST /tts/play.
        self._speak_lock = threading.Lock()
        # "Angkatan" per perangkat: naik tiap perangkat masuk mode hemat daya. Pekerjaan yang
        # dimulai sebelum itu (deteksi, TTS, kirim audio) menjadi basi dan dibuang, bukan
        # diucapkan terlambat setelah pengguna menekan tombol.
        self._epoch_lock = threading.Lock()
        self._epochs = {}
        # Jeda deteksi objek otomatis (sisi server), dihitung setelah deteksi selesai.
        self._auto_lock = threading.Lock()
        self._auto_object_next_ok = 0.0
        self._audio_slots = threading.BoundedSemaphore(8)
        self._output_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="quick-output")
        self._button_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quick-button")
        self.jobs = {}
        self.audio_dir = BASE_DIR / "audio_storage"
        self.audio_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------- sensor
    def fresh_sensor(self, sid):
        state = self.sessions.get(sid) or {}
        values = [(t, d) for t, d in state.get("distance_samples", []) if 0 <= time.time() - t <= 4.0]
        if not values:
            return None
        # Jangan menghaluskan halangan yang tiba-tiba lebih dekat.
        distance = min(values[-1][1], sorted(d for _, d in values)[len(values) // 2])
        return {"distance_cm": distance, "source": "ultrasonic",
                "age_ms": int((time.time() - values[-1][0]) * 1000), "associated_object": None}

    # ------------------------------------------------- pembatalan saat ganti mode
    def current_epoch(self, device_id):
        with self._epoch_lock:
            return self._epochs.get(device_id, 0)

    def on_mode_change(self, device_id, mode):
        """Perangkat melaporkan mode. Masuk hemat daya = batalkan semua keluaran yang tertunda."""
        if mode == "normal" or not device_id:
            return
        with self._epoch_lock:
            self._epochs[device_id] = self._epochs.get(device_id, 0) + 1
        self.log.info("[mode] %s masuk %s: deteksi/audio yang masih berjalan dibatalkan", device_id, mode)

    def _cancelled(self, device_id, epoch):
        return epoch is not None and bool(device_id) and self.current_epoch(device_id) != epoch

    # --------------------------------------------------------------- audio
    def _cleanup_jobs(self):
        cutoff = time.monotonic() - 300
        with self._jobs_lock:
            for key in [k for k, v in self.jobs.items() if v["created"] < cutoff and v["status"] != "pending"]:
                self.jobs.pop(key, None)
        # Hanya berkas keluaran API ini, setelah satu jam masa putar ulang.
        for path in self.audio_dir.glob("quick_*.mp3"):
            try:
                if time.time() - path.stat().st_mtime > 3600:
                    path.unlink()
            except OSError:
                pass

    def _device_online(self, sid):
        device = self.sessions.device_for_session(sid)
        online = bool(device and self.push_device and self.sessions.get_esp32_socket(device))
        return device, online

    def _output_worker(self, sid, rid, data, from_camera, epoch=None):
        key = (sid, rid)
        try:
            audio = self.audio_getter()
            if audio is None:
                raise RuntimeError("TTS tidak tersedia")
            name = f"quick_{sid}_{rid}.mp3"
            mp3_bytes = audio.generate_tts(data["result"]).getvalue()
            (self.audio_dir / name).write_bytes(mp3_bytes)
            device, online = self._device_online(sid)
            url = "/api/quick_audio_file/" + name
            # Perintah yang lebih baru membatalkan audio dari TTS lambat sebelumnya.
            if (self.sessions.get(sid) or {}).get("quick_latest_request") != rid:
                state = {"status": "superseded"}
            elif self._cancelled(device, epoch):
                state = {"status": "cancelled", "message": "Mode hemat daya: suara dibatalkan."}
            elif online:
                esp32_ip = self.sessions.get_device_ip(device)
                self.log.info(
                    "[quick_commands] tombol -> push audio: device=%s esp32_ip=%s "
                    "mp3_bytes=%d sid=%s rid=%s", device, esp32_ip, len(mp3_bytes), sid, rid)
                with self._speak_lock:
                    # Cek ulang tepat sebelum kirim: menunggu giliran bisa memakan waktu.
                    pushed = (bool(esp32_ip) and not self._cancelled(device, epoch)
                              and push_mp3_to_esp32(mp3_bytes, esp32_ip))
                cancelled = self._cancelled(device, epoch)
                if not pushed and not cancelled:
                    self.log.warning(
                        "[quick_commands] push audio GAGAL, fallback ke speaker laptop: "
                        "device=%s esp32_ip=%s sid=%s rid=%s", device, esp32_ip, sid, rid)
                if pushed:
                    state = {"status": "ready", "audio_url": url, "audio_output": "esp32"}
                elif cancelled:
                    state = {"status": "cancelled", "message": "Mode hemat daya: suara dibatalkan."}
                elif from_camera and not self.settings.esp32_audio_local_fallback:
                    # Jangan pindah ke speaker laptop: beri tahu perangkat lewat beep saja.
                    try:
                        self.push_device(device, {"type": "error", "message": "audio TWS gagal"})
                    except Exception:
                        self.log.debug("Gagal mengirim beep error ke perangkat", exc_info=True)
                    state = {"status": "ready", "audio_url": url, "audio_output": "none"}
                elif from_camera:
                    played = bool(audio._play_audio(str(self.audio_dir / name)))
                    state = {"status": "ready", "audio_url": url,
                             "audio_output": "local" if played else "none"}
                else:
                    state = {"status": "ready", "audio_url": url, "audio_output": "browser"}
            elif from_camera:
                played = bool(audio._play_audio(str(self.audio_dir / name)))
                state = {"status": "ready", "audio_url": url, "audio_output": "local" if played else "none"}
            else:
                state = {"status": "ready", "audio_url": url, "audio_output": "browser"}
        except Exception:
            state = {"status": "error", "message": "TTS tidak tersedia; gunakan suara browser."}
        finally:
            with self._jobs_lock:
                if key in self.jobs:
                    self.jobs[key].update(state)
        # Kegagalan database tidak boleh membatalkan hasil visual yang sukses.
        try:
            database = self.database_getter()
            if database:
                database.save_interaction({
                    "session_id": sid, "request_id": rid, "mode": data["mode"],
                    "prompt": f"Quick Command: {data['mode']}", "response": data["result"],
                    "detected_objects": data["detected_objects"], "sign_text": data["sign_text"],
                    "sign_confidence": data["sign_confidence"]})
        except Exception:
            self.log.exception("Gagal menyimpan hasil quick command")
        finally:
            self._audio_slots.release()

    def announce(self, sid, text, *, voice=True):
        """Umumkan kegagalan ke speaker ESP32 (suara bila bisa, beep rendah bila tidak).

        Pengguna tunanetra tidak boleh mengalami kegagalan yang senyap.
        """
        device, online = self._device_online(sid)
        if not online:
            return False
        if self.sessions.get_device_mode(device) == "hemat":
            return False         # mode hemat daya: tidak ada suara dari backend
        if voice and self.speak(sid, text):
            return True
        self.push_device(device, {"type": "error", "message": text})
        return True

    def speak(self, sid, text, epoch=None):
        """Ucapkan ``text`` di TWS lewat ESP32 (TTS -> push PCM). ``True`` bila terkirim.

        Dipakai pengumuman dan SignWatcher. Tidak ada fallback beep: pemanggil
        yang memutuskan (``announce`` memakai beep rendah bila gagal).
        """
        device, online = self._device_online(sid)
        if not online or not text:
            return False
        if epoch is None:
            epoch = self.current_epoch(device)
        try:
            audio = self.audio_getter()
            if audio is None:
                return False
            mp3_bytes = audio.generate_tts(text).getvalue()
            esp32_ip = self.sessions.get_device_ip(device)
            if not esp32_ip:
                return False
            with self._speak_lock:
                if self._cancelled(device, epoch):
                    return False     # pengguna pindah ke mode hemat selagi TTS dibuat/menunggu giliran
                return bool(push_mp3_to_esp32(mp3_bytes, esp32_ip))
        except Exception:
            self.log.warning("TTS/pengiriman suara gagal", exc_info=True)
            return False

    # ---------------------------------------------------------------- core
    def run(self, sid, rid, mode, *, frames=None, camera_id="", wait_seconds=0.0):
        """Jalankan satu mode. Kembalikan ``(payload, http_status)``.

        ``frames=None`` berarti memotret lewat kamera server (ESP32-CAM).
        ``wait_seconds`` > 0: tunggu sebentar bila jalur yang sama sedang dipakai
        alih-alih langsung menolak.
        """
        self.sessions.create_session(sid)
        status, cached = self.sessions.begin_request(sid, rid, prompt_key="", repeat_window_seconds=0)
        if status == "cached":
            return dict(cached), 200
        if status == "in_progress":
            return {"status": "busy", "message": "Permintaan sebelumnya masih berjalan"}, 409
        vision_lock = self._vision_locks.get(mode)
        if vision_lock is None:
            self.sessions.fail_request(sid, rid)
            return {"status": "error", "message": "Mode harus objek atau rambu"}, 400
        acquired = (vision_lock.acquire(timeout=wait_seconds) if wait_seconds > 0
                    else vision_lock.acquire(blocking=False))
        if not acquired:
            self.sessions.fail_request(sid, rid)
            return {"status": "busy", "message": f"{MODE_LABEL.get(mode, mode)} sedang berjalan. Coba kembali sebentar lagi."}, 429
        try:
            expected = self.pipeline.frame_count(mode)
            self.sessions.update(sid, quick_latest_request=rid)
            epoch = self.current_epoch(self.sessions.device_for_session(sid))
            from_camera = frames is None and self.camera is not None
            if from_camera:
                frames = self.camera.capture_frames(expected)
            elif frames is None or not 1 <= len(frames) <= expected:
                raise ValueError(f"Kirim 1 sampai {expected} snapshot untuk mode ini")
            received_at = time.time()
            result = self.pipeline.run(mode, frames, camera_id=camera_id or ("esp32cam" if from_camera else ""))
            # Inferensi bisa makan detik: cek kesegaran sensor saat selesai.
            if mode == "objek":
                sensor = self.fresh_sensor(sid)
                result["sensor_distance"] = sensor
                result["result"] = self.pipeline.spatial.summarize(result["detected_objects"], sensor)
                if not result.get("scene_available"):
                    result["result"] += " Pengenalan tembok dan pintu tidak aktif."
            result["status"] = "partial" if result.get("errors") else "success"
            result.update(request_id=rid, received_at=received_at, completed_at=time.time())
            if from_camera:
                ok, encoded = cv2.imencode(".jpg", frames[-1], [cv2.IMWRITE_JPEG_QUALITY, 88])
                if ok:
                    result["image_url"] = "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
            self.sessions.update(
                sid, mode=mode, last_result=result["result"], last_objects=result["detected_objects"],
                last_signs=result["sign_boxes"], last_sign_text=result["sign_text"],
                last_sign_confidence=result["sign_confidence"], last_sign_reliable=result["sign_reliable"],
                last_vision_at=time.time())
            self._cleanup_jobs()
            if self._audio_slots.acquire(blocking=False):
                with self._jobs_lock:
                    self.jobs[(sid, rid)] = {"status": "pending", "created": time.monotonic()}
                self._output_pool.submit(self._output_worker, sid, rid, dict(result), from_camera, epoch)
                result["audio_job"] = f"/api/quick_audio/{sid}/{rid}"
            else:
                result["warnings"].append("Antrean audio penuh; gunakan suara browser. Pencatatan hasil ini dilewati.")
            self.sessions.finish_request(sid, rid, "", result)
            return result, 200
        except ValueError as exc:
            self.sessions.fail_request(sid, rid)
            return {"status": "error", "message": str(exc)}, 400
        except CameraError as exc:
            self.sessions.fail_request(sid, rid)
            self.log.warning("Kamera gagal: %s", exc)
            return {"status": "error", "message": str(exc), "spoken": CAMERA_DOWN_SPOKEN}, 503
        except Exception as exc:
            self.sessions.fail_request(sid, rid)
            self.log.exception("Quick command gagal")
            return {"status": "error", "message": f"Mode {mode} belum tersedia: {exc}",
                    "spoken": f"{MODE_LABEL.get(mode, mode)} belum tersedia."}, 503
        finally:
            vision_lock.release()

    # ------------------------------------------------------- tombol fisik
    def start_from_button(self, *, mode, device_id, session_id, label="", source="button",
                          auto=False):
        """Mulai satu perintah tanpa memblokir thread WebSocket.

        Dipakai uji manual (``/api/esp32/event``) dan, dengan ``auto=True``, pemicu
        otomatis sensor ultrasonik (menunggu AI bebas sebentar, lalu memulai jeda).
        """
        if not self._button_gate.acquire(blocking=False):
            return {"status": "busy", "reason": "perintah sebelumnya masih berjalan"}
        rid = f"{'auto' if auto else 'btn'}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        try:
            self._button_pool.submit(self._button_job, rid, session_id, mode, device_id, label, auto)
        except Exception:
            self._button_gate.release()
            raise
        return {"status": "started", "request_id": rid, "session_id": session_id}

    # ------------------------------------------- deteksi objek dari sensor
    def handle_distance(self, *, device_id, session_id, distance_cm):
        """Sampel jarak ultrasonik -> deteksi objek otomatis (hanya mode biasa).

        Jeda ada di SINI (server): selama ``OBJECT_AUTO_COOLDOWN_SECONDS`` sejak
        deteksi terakhir selesai, sampel jarak berikutnya diabaikan, jadi benda
        yang terus berada di jarak 1 m tidak membuat kamera mendeteksi berulang.
        """
        cfg = self.settings
        if self.sessions.get_device_mode(device_id) != "normal":
            return {"status": "ignored", "reason": "mode hemat daya"}
        if not 0 < distance_cm <= cfg.object_trigger_cm:
            return {"status": "ignored", "reason": "di luar jarak pemicu"}
        with self._auto_lock:
            remaining = self._auto_object_next_ok - time.monotonic()
        if remaining > 0:
            return {"status": "cooldown", "retry_in_seconds": round(remaining, 1)}
        outcome = self.start_from_button(
            mode="objek", device_id=device_id, session_id=session_id,
            label=MODE_LABEL["objek"], source="ultrasonic", auto=True)
        if outcome.get("status") == "started":
            # Jeda mulai SEKARANG (bukan menunggu deteksi selesai), jadi deteksi yang
            # lama/gagal pun tidak bisa memicu potret ulang sebelum jeda habis.
            # Saat deteksi selesai, jeda dihitung ulang dari waktu selesai.
            self._start_auto_cooldown()
            self.log.info("[auto] deteksi objek dipicu sensor: device=%s jarak=%.0f cm", device_id, distance_cm)
        return outcome

    def _start_auto_cooldown(self):
        with self._auto_lock:
            self._auto_object_next_ok = time.monotonic() + max(0.0, self.settings.object_auto_cooldown_seconds)

    # ----------------------------------------------- deteksi rambu berkala
    def detect_signs_once(self):
        """Satu siklus deteksi rambu: potret 1 frame + YOLO. ``None`` bila siklus rambu sebelumnya masih jalan.

        Tidak bergantung pada deteksi objek: memakai kunci rambu sendiri.

        Melempar ``CameraError`` bila kamera mati dan ``RuntimeError`` bila model gagal.
        """
        if self.camera is None:
            raise CameraError("Kamera belum dikonfigurasi (ESP32CAM_URL).")
        lock = self._vision_locks["rambu"]
        if not lock.acquire(blocking=False):
            return None          # siklus rambu lain (mis. uji manual) masih berjalan; lewati
        try:
            # save=False: siklus berkala TIDAK boleh menyimpan frame ke disk (walau SAVE_CAPTURES=true).
            frames = self.camera.capture_frames(1, save=False)
            # save_image=False: siklus berkala (tiap ~1 dtk) tidak boleh menumpuk file di disk.
            return self.pipeline.run("rambu", frames, camera_id="esp32cam", save_image=False)
        finally:
            lock.release()

    def _button_job(self, rid, sid, mode, device_id, label, auto=False):
        payload, code = {}, 500
        try:
            payload, code = self.run(sid, rid, mode, wait_seconds=5.0 if auto else 0.0)
            if code != 200:
                if code in (409, 429):
                    self.announce(sid, payload.get("message", ""), voice=False)
                else:
                    self.announce(sid, payload.get("spoken") or f"{label or mode} gagal.")
        except Exception:
            self.log.exception("Perintah tombol gagal")
            try:
                self.announce(sid, "Terjadi kesalahan pada sistem.")
            except Exception:
                pass
        finally:
            # Jeda dimulai begitu deteksi selesai (berhasil atau gagal), kecuali
            # karena AI sibuk: itu belum deteksi, biarkan sampel berikutnya mencoba.
            if auto and code not in (409, 429):
                self._start_auto_cooldown()
            elif auto:
                with self._auto_lock:             # tidak ada deteksi yang jalan: batalkan jeda
                    self._auto_object_next_ok = 0.0
            self._button_gate.release()
            try:
                self.notify({"type": "quick_result", "mode": mode, "label": label, "device_id": device_id,
                             "request_id": rid, "http_status": code,
                             "result": payload.get("result") or payload.get("message", ""),
                             "processing_ms": payload.get("processing_ms")})
            except Exception:
                pass


def register_quick_commands(app, services, sessions, audio_getter, database_getter,
                            camera=None, push_device=None, notify=None, settings=None):
    """Pasang endpoint ``/api/quick*`` dan kembalikan ``QuickCommandService``."""
    service = QuickCommandService(services, sessions, audio_getter, database_getter,
                                  camera=camera, push_device=push_device, notify=notify,
                                  logger=app.logger, settings=settings)
    api = Blueprint("quick", __name__)
    max_pixels = 6_000_000
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

    def decode(upload):
        raw = upload.read()
        try:
            with Image.open(io.BytesIO(raw)) as im:
                if im.width * im.height > max_pixels or min(im.size) < 32:
                    raise ValueError("Ukuran snapshot harus 32 piksel atau lebih dan maksimal 6 megapiksel")
                if max(im.size) / min(im.size) > 8:
                    raise ValueError("Rasio snapshot tidak didukung; gunakan rasio kamera biasa")
                if im.format not in {"JPEG", "PNG"}:
                    raise ValueError("Snapshot harus JPEG/PNG")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Snapshot tidak valid") from exc
        frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("Snapshot tidak dapat dibaca")
        return frame

    @api.get("/api/client_config")
    def client_config():
        return jsonify(
            camera_source="server",         # kamera selalu milik backend (ESP32-CAM), bukan browser
            object_frames=service.pipeline.burst_count, burst_gap_ms=100, max_snapshot_width=1920,
            services={name: svc.status() for name, svc in services.items()})

    @api.post("/api/quick")
    def quick():
        sid = request.form.get("session_id", "")
        rid = request.form.get("request_id", "")
        mode = request.form.get("mode", "")
        if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", v) for v in (sid, rid)):
            return jsonify(status="error", message="session_id/request_id tidak valid"), 400
        if mode not in MODE_LABEL:
            return jsonify(status="error", message="Mode tidak valid"), 400
        try:
            files = request.files.getlist("image")
            frames = [decode(f) for f in files] if files else None
        except ValueError as exc:
            return jsonify(status="error", message=str(exc)), 400
        payload, code = service.run(sid, rid, mode, frames=frames,
                                    camera_id=request.form.get("camera_id", ""))
        return jsonify(payload), code

    @api.get("/api/quick_audio/<sid>/<rid>")
    def audio_status(sid, rid):
        with service._jobs_lock:
            value = dict(service.jobs.get((sid, rid), {"status": "expired"}))
        value.pop("created", None)
        return jsonify(value)

    @api.get("/api/quick_audio_file/<name>")
    def audio_file(name):
        if not re.fullmatch(r"quick_[A-Za-z0-9_-]+\.mp3", name):
            return jsonify(status="error"), 404
        return send_from_directory(service.audio_dir, name, mimetype="audio/mpeg", conditional=False)

    # -------------------------------------------------- galeri hasil deteksi
    # Hanya aktif bila SAVE_DETECTION_IMAGES=true di .env. Untuk dilihat
    # manusia lewat browser saja; tidak dipakai alur AI/TTS mana pun.
    @api.get("/api/detection_images")
    def detection_images():
        pipeline = service.pipeline
        if not pipeline.save_detection_images:
            return jsonify(status="disabled",
                            message="Set SAVE_DETECTION_IMAGES=true di .env untuk mengaktifkan."), 200
        directory = pipeline.detection_image_dir
        try:
            names = sorted(
                (n for n in os.listdir(directory) if n.endswith(".jpg")),
                key=lambda n: os.path.getmtime(os.path.join(directory, n)),
                reverse=True,
            )
        except OSError:
            names = []
        limit = min(200, max(1, int(request.args.get("limit", 50))))
        items = [{"name": n, "url": f"/api/detection_image_file/{n}"} for n in names[:limit]]
        return jsonify(status="success", count=len(names), items=items)

    @api.get("/api/detection_image_file/<name>")
    def detection_image_file(name):
        pipeline = service.pipeline
        if not re.fullmatch(r"(objek|rambu)_[0-9_]+\.jpg", name):
            return jsonify(status="error"), 404
        return send_from_directory(pipeline.detection_image_dir, name, mimetype="image/jpeg", conditional=False)

    @api.get("/detections")
    def detection_gallery():
        """Halaman HTML sederhana untuk melihat galeri hasil deteksi di browser."""
        return Response(_DETECTION_GALLERY_HTML, mimetype="text/html")

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(status="error", message="Upload terlalu besar; maksimum 16 MiB per perintah."), 413

    app.register_blueprint(api)
    app.extensions["quick_service"] = service
    return service