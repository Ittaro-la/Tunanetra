# Arsitektur

## 1. Alur

```
Sensor ultrasonik ──► ESP32 utama ──WebSocket /ws──► Backend Python ──HTTP GET──► ESP32-CAM
 (≈1 m, mode biasa)        ▲                              │ YOLOv5n · SegFormer · YOLO rambu
                           │  POST /tts/play (PCM)        │ TTS Edge/gTTS
                           └──────────────────────────────┘
ESP32 ──Bluetooth A2DP──► TWS / headphone (suara)

Deteksi rambu: thread SignWatcher di backend, terus-menerus (mode biasa) ──► ESP32-CAM ──► suara
```

Satu tombol push mengganti mode (biasa ↔ hemat daya); satu saklar on/off memutus
arus listrik ESP32 (tanpa kode).

## 2. Mode

| | **Mode biasa** | **Mode hemat daya** |
|---|---|---|
| Ultrasonik | aktif; jarak ≤ ~1 m dikirim ke backend → deteksi objek | aktif, hanya lokal |
| Deteksi objek | ya, dipicu ultrasonik, **jeda 30–60 dtk di server** | tidak |
| Deteksi rambu (arah jam) | ya, terus-menerus | tidak (kamera tidak dipakai) |
| WebSocket | tersambung | **diputus** (disambung lagi saat tombol ditekan) |
| Buzzer | berbunyi terus tiap interval tetap (penanda lokasi pengguna) | mengikuti jarak: makin dekat, makin cepat |
| LED | berkedip tiap interval (penanda lokasi) | berkedip tiap interval |

Mode disimpan di firmware dan dikirim saat `register`. Masuk mode hemat = firmware memutus WebSocket,
jadi backend melihat perangkat offline: SignWatcher berhenti dan tidak ada pemicu objek. Kembali ke mode
biasa = firmware menyambung ulang dan `register` membawa `mode: normal`. (Pesan `mode_change` masih
diterima backend, tetapi firmware tidak lagi memakainya.)

## 3. Apa berjalan di mana

| Komponen | Tugas |
|---|---|
| **ESP32-CAM** (`firmware/esp32_cam/main.py`, MicroPython) | setiap request HTTP dibalas satu JPEG segar (VGA 640×480) |
| **ESP32 utama** (`firmware/esp32_multidevice/esp32_spider`) | tombol mode, ultrasonik HC-SR04, buzzer, LED, Wi-Fi + WebSocket, menerima PCM lalu mengirimnya ke TWS lewat Bluetooth A2DP |
| **Backend** (`main.py`) | pemicu otomatis, YOLO objek/rambu, SegFormer, arah jam, TTS, jeda deteksi |

## 4. Protokol

ESP32 → backend (WebSocket `/ws`, JSON):

| Pesan | Arti |
|---|---|
| `{"type":"register","role":"esp32","device_id":"esp32-A1","ip":"…","mode":"normal"}` | daftar; backend menyimpan IP (untuk push suara) dan mode |
| `{"type":"mode_change","mode":"hemat"}` | tombol ditekan; `normal` atau `hemat` |
| `{"type":"quick_command","event":"obstacle_warning","distance_cm":92}` | sampel jarak (hanya dikirim di mode biasa saat ≤ ~1 m, maks. 1×/dtk). Tidak dibalas |

Backend → ESP32: `registered`, `error` (beep rendah). Suara: `POST http://<ip-esp32>:8080/tts/play`
(PCM 16-bit, 44,1 kHz, stereo, header `X-Auth-Token`).

Uji manual: `POST /api/esp32/event {"event":"object_detection"|"sign_detection"}`.

## 5. Deteksi objek dari ultrasonik

`QuickCommandService.handle_distance`: mode harus `normal`, jarak ≤ `OBJECT_TRIGGER_CM` (100), dan
jeda `OBJECT_AUTO_COOLDOWN_SECONDS` (60) sejak deteksi dimulai **dan** sejak deteksi sebelumnya selesai harus lewat. Selama jeda,
sampel jarak diabaikan, jadi benda yang diam di 1 m tidak membuat kamera mendeteksi terus-menerus.
Bila AI sedang dipakai, pemicu menunggu ≤ 5 dtk dan jeda belum dimulai.

## 6. Deteksi rambu: jarak dan arah jam

`ai/sign_geometry.py`, dipanggil `ai/pipeline.py`, dijalankan berkala oleh `services/sign_watch.py`.

* **Jarak**: kalibrasi satu titik, `SIGN_REF_PIXELS=85` px = `SIGN_REF_DISTANCE_M=2.0` m pada gambar selebar
  `SIGN_REF_FRAME_WIDTH=640`. `jarak = 2.0 × 85 / sisi_terpanjang_kotak` (dinormalkan bila resolusi lain).
* **Arah jam**: dari titik pusat gambar ke titik pusat kotak. Atas = jam 12, kanan = jam 3, bawah = jam 6,
  kiri = jam 9. Bila `SIGN_IMAGE_MIRRORED=true` (firmware kamera memakai `camera.mirror(1)`) kiri-kanan dibalik.
* **Ucapan**: `<nama rambu> berada pada arah jam N, sekitar X meter.`
* **Tidak cerewet**: rambu harus terlihat 2 siklus berturut-turut; rambu yang sama diumumkan lagi
  setelah 20 dtk atau bila arah jamnya berubah ≥ 2 jam.

## 7. Ketahanan

| Kegagalan | Perilaku |
|---|---|
| ESP32-CAM mati | deteksi objek: "Kamera tidak terhubung…"; SignWatcher: diumumkan sekali per gangguan (setelah 5 gagal), backoff |
| Model gagal dimuat | diucapkan sekali; perintah berikutnya tidak terkunci |
| Pemicu saat proses lain berjalan | objek otomatis menunggu ≤ 5 dtk; uji manual ditolak (beep rendah) |
| Internet mati | deteksi jalan, tetapi **TTS online tidak tersedia** → hanya beep (titik lemah terbesar) |

## 8. Batasan

* Kamera monokular: jarak dari ukuran kotak hanya perkiraan dan bergantung pada kalibrasi; ukur ulang
  `SIGN_REF_PIXELS` di lapangan. Hasil kosong bukan berarti jalur aman.
* Latensi: tangkap + YOLO CPU + TTS + push + Bluetooth ≈ beberapa detik.
* Tanpa autentikasi/HTTPS pada backend dan ESP32-CAM: pakai jaringan tepercaya.
