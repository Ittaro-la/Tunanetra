# Firmware ESP32-CAM (MicroPython) — aktif dipakai

Firmware ESP32-CAM project ini adalah **MicroPython** (`main.py`), yang sudah
teruji jalan lewat Thonny.

```
firmware/esp32_cam/
  micropython_v1.21.0_camera_no_ble.bin   firmware dasar MicroPython (flash sekali via esptool.py)
  main.py                                  <-- AKTIF: kode kamera (upload via Thonny)
  README.md                                file ini
```

## Cara pakai

1. **Flash firmware dasar MicroPython** (sekali saja, atau kalau board belum pernah dipakai MicroPython):
   ```bash
   pip install esptool
   esptool.py --chip esp32 --port <PORT> erase_flash
   esptool.py --chip esp32 --port <PORT> --baud 460800 write_flash -z 0x1000 micropython_v1.21.0_camera_no_ble.bin
   ```
2. **Buka Thonny**, pilih interpreter "MicroPython (ESP32)" dan port board.
3. Edit `ssid` / `password` di `main.py` supaya sama dengan jaringan WiFi laptop (backend).
4. Simpan `main.py` ke board (Thonny: File → Save as → MicroPython device → beri nama `main.py`
   supaya otomatis jalan tiap boot), lalu jalankan/reset board.
5. Catat IP yang muncul di konsol Thonny, contoh `Terhubung ke Wi-Fi. IP Address: 192.168.1.60`.
6. Isi di `.env` backend (root project):
   ```
   CAMERA_SOURCE=esp32cam
   ESP32CAM_URL=http://192.168.1.60
   ```
   (`ESP32CAM_TOKEN` dan `ESP32CAM_FLASH` boleh dikosongkan — firmware ini tidak mendukungnya.)

## Uji tanpa menjalankan backend penuh

```bash
python tools/check_esp32cam.py http://<ip> --frames 3 --save
```

## Cara kerja server di firmware

Server HTTP di `main.py` **tidak membaca path maupun method** dari request —
setiap koneksi TCP yang masuk langsung dibalas satu JPEG segar dari
`camera.capture()`. Karena itu tidak ada `/capture` atau `/status` terpisah,
dan tidak ada pemeriksaan token. Adapter Python-nya ada di
`camera/esp32cam.py` (root project) dan sudah disesuaikan dengan perilaku ini.
