# Spider Sense — alat bantu tunanetra berbasis ESP32

Dua deteksi berjalan otomatis; hasilnya **diucapkan di TWS/headphone** lewat ESP32.

```
Ultrasonik (≈1 m) ─► ESP32 ──WebSocket (Wi-Fi)──► Backend Python ──HTTP──► ESP32-CAM
                       │                              │ YOLOv5n · SegFormer · YOLO rambu · TTS
                       ◄──── POST /tts/play (PCM) ────┘
                       └── Bluetooth A2DP ──► TWS / headphone

Deteksi rambu: berjalan terus-menerus di backend ──► "Dilarang Parkir berada pada arah jam 11, sekitar 2 meter."
```

| Perangkat keras | Fungsi |
|---|---|
| 1 tombol push | ganti mode: **biasa** ↔ **hemat daya** |
| 1 saklar on/off | memutus arus listrik ESP32 (hardware, tanpa kode) |
| Ultrasonik, buzzer, LED | jarak, bunyi, penanda lokasi |

| | Mode biasa | Mode hemat daya |
|---|---|---|
| Deteksi objek | dipicu ultrasonik ≈ 1 m, jeda 30–60 dtk (di server) | tidak ada |
| Deteksi rambu + arah jam | terus-menerus | tidak ada |
| Buzzer | berbunyi tiap interval (penanda lokasi pengguna) | makin dekat objek, makin cepat |
| LED | berkedip tiap interval | berkedip tiap interval |

> **Batas penting.** Ini prototipe. Kamera monokular tidak mengukur jarak absolut (jarak rambu hanya
> perkiraan dari satu titik kalibrasi), dan hasil kosong bukan berarti jalur aman. Jangan jadikan satu-satunya
> alat navigasi. Server belum punya autentikasi/HTTPS: jalankan hanya di jaringan tepercaya.

## Struktur

```
main.py                 titik masuk:  python main.py
config/settings.py      semua pengaturan (.env)
ai/                     object_detection, sign_detection, sign_geometry (jarak + arah jam), pipeline
camera/                 esp32cam.py (utama), webcam.py (opsional, uji meja)
devices/                ws.py (WebSocket ESP32), events.py (pemicu manual)
services/               quick_commands (orkestrasi + jeda), sign_watch (rambu terus-menerus), sessions, audio, database
server/                 app.py (perakitan Flask), upload.py (tanya-jawab opsional)
firmware/               esp32_multidevice/esp32_spider (ESP32 utama) · esp32_cam (MicroPython)
model/  web/  tools/  tests/  docs/
```

## Menjalankan

1. **Backend** (Python 3.11/3.12):
   ```bash
   python -m venv .venv 
   .venv\Scripts\activate
   pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
   pip install -r requirements.txt
   cp .env.example .env          # isi ESP32CAM_URL, kunci Groq/Mongo/Ubidots bila dipakai
   python tools/prepare_models.py   # sekali: unduh SegFormer (tembok/pintu)
   python tools/check_esp32cam.py http://<ip-kamera> --save   # uji kamera tanpa backend
   python main.py
   ```
2. **Firmware**:
   - ESP32-CAM: `firmware/esp32_cam/main.py` (MicroPython, via Thonny) — lihat `docs/ESP32CAM.md`
     atau `firmware/esp32_cam/README.md`.
   - ESP32 utama: `firmware/esp32_multidevice/esp32_spider/` (Arduino, board ESP32 klasik dual-mode).
     Salin `secrets.h.example` → `secrets.h`; `SERVER_HOST` = IP komputer backend; `TWS_DEVICE_NAME` =
     nama Bluetooth TWS. Pin dan interval ada di bagian KONFIGURASI sketsa.
3. **Uji tanpa perangkat keras**: `python tools/check_esp32_event.py --host <ip> --event object_detection`.

Tes: `pytest -q` (Node ≥ 18 untuk `node tests/test_frontend.cjs`). Tes yang memuat model asli
membutuhkan `torch` terpasang.

## Dokumentasi

- `docs/ARSITEKTUR.md` — mode, protokol, jeda deteksi, jarak + arah jam rambu, batasan
- `docs/ESP32CAM.md` — perakitan, flashing, konfigurasi, pemecahan masalah kamera
- `docs/AI_DETAIL.md` — perilaku deteksi, ambang, variabel `.env`
