"""Titik masuk tunggal Spider Sense (backend headless).

    python main.py

Tidak membutuhkan browser maupun webcam. Mode biasa: sensor ultrasonik ESP32
memicu deteksi objek (dengan jeda), dan deteksi rambu berjalan terus-menerus;
hasilnya diucapkan di TWS lewat ESP32. Mode hemat daya: tanpa deteksi.
"""
from __future__ import annotations

import threading

from config.settings import settings
from server.app import create_app


def main():
    app, ctx = create_app()
    cam = ctx.camera.get_status()["source"] if ctx.camera is not None else "BELUM DIKONFIGURASI (isi ESP32CAM_URL)"
    print(f"[Kamera]  {cam}")
    print(f"[Objek]   dipicu ultrasonik <= {settings.object_trigger_cm:.0f} cm, "
          f"jeda {settings.object_auto_cooldown_seconds:.0f} dtk")
    print(f"[Rambu]   {'terus-menerus' if settings.sign_watch_enabled else 'NONAKTIF'}, "
          f"jarak ref {settings.sign_ref_pixels:.0f}px = {settings.sign_ref_distance_m}m")
    print(f"[ESP32]   WebSocket ws://<ip-backend>:{settings.port}/ws | uji manual: POST /api/esp32/event")
    ctx.sign_watcher.start()
    if settings.web_ui_enabled:
        print(f"[Monitor] halaman web opsional: http://localhost:{settings.port}")
    threading.Thread(target=ctx.background_task, daemon=True).start()
    app.run(host=settings.host, port=settings.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
