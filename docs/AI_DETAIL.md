# Spider Sense — detail deteksi objek dan rambu

## Instalasi dan menjalankan

Lihat `README.md`. Model dimuat sekali sesuai kebutuhan dan dibagi semua sesi.
`python tools/prepare_models.py` mengunduh SegFormer (tembok/pintu), satu kali.

## Dua deteksi

| Deteksi | Pemicu | Proses | Hasil |
|---|---|---|---|
| Objek | ultrasonik ≈ 1 m (mode biasa); jeda 30–60 dtk di server | Burst 3 frame → YOLOv5n → konfirmasi antargambar; SegFormer sekali di frame akhir | Objek/area, posisi (kiri/tengah/kanan), kemungkinan halangan |
| Rambu | terus-menerus (mode biasa), 1 frame per siklus | YOLO rambu (`model/rambu_yolo.pt`, 40 kelas) → warna lampu lalu lintas → jarak → arah jam | "<rambu> berada pada arah jam N, sekitar X meter." |

Mode hemat daya tidak menjalankan keduanya. Satu pekerjaan vision aktif per proses: siklus rambu
dilewati saat deteksi objek berjalan, dan deteksi objek otomatis menunggu ≤ 5 dtk bila siklus rambu
sedang berjalan. Cara hitung jarak dan arah jam: `docs/ARSITEKTUR.md` bagian 6.

Kotak hasil ada pada **snapshot yang dianalisis**, bukan overlay di video. Ini bukan peringatan
navigasi realtime dan hasil kosong bukan berarti jalur aman.

## Tembok/pintu

SegFormer-B0 (ADE20K) menambah tembok/pintu pada mode objek. Memakai confidence piksel, luas area, dan
perpotongan mask dengan koridor tengah-bawah. Pintu terdeteksi belum tentu tertutup; tembok jauh belum
tentu bahaya dekat. `SCENE_SEGMENTATION=false` mematikannya (RAM/latency lebih ringan, cakupan hilang).

**Lisensi SegFormer yang dirujuk dibatasi untuk penelitian/evaluasi nonkomersial**; source YOLOv5 AGPL-3.0.
Periksa sebelum dipakai komersial.

## Confidence yang jujur

* YOLO: skor model sesudah NMS/filter, bukan akurasi dunia nyata. Burst memakai median skor asli.
* SegFormer: mean probabilitas piksel pada area; tidak sebanding langsung dengan skor YOLO.
* Kiri/tengah/kanan dan arah jam adalah posisi dalam gambar, bukan pemetaan 3D.
* Jarak rambu dari ukuran kotak hanya perkiraan satu titik kalibrasi.

## Ultrasonik

Jarak dikaitkan ke sesi ESP32, kedaluwarsa 4 dtk, menolak nilai non-angka/di luar 2–500 cm. Jarak sensor
tidak ditempelkan ke objek YOLO (bidang pandang berbeda). `OBSTACLE_WARNING_CM=50` hanya ambang
perhatian dalam ringkasan, bukan jangkauan kamera.

## Konfigurasi `.env`

Deteksi otomatis:

| Variabel | Default | Fungsi |
|---|---:|---|
| `OBJECT_TRIGGER_CM` | 100 | Jarak ultrasonik pemicu deteksi objek |
| `OBJECT_AUTO_COOLDOWN_SECONDS` | 60 | Jeda antar deteksi objek otomatis (dihitung dari mulai dan dari selesai) |
| `SIGN_WATCH_ENABLED` | true | Deteksi rambu terus-menerus |
| `SIGN_WATCH_INTERVAL_SECONDS` | 1.0 | Jeda antar siklus rambu |
| `SIGN_ANNOUNCE_COOLDOWN_SECONDS` | 20 | Jeda mengumumkan rambu yang sama |
| `SIGN_CONFIRM_FRAMES` | 2 | Siklus berturut-turut sebelum diumumkan |
| `SIGN_REF_PIXELS` / `SIGN_REF_DISTANCE_M` | 85 / 2.0 | Kalibrasi: sisi terpanjang kotak = 85 px pada 2 m |
| `SIGN_REF_FRAME_WIDTH` | 640 | Lebar gambar saat kalibrasi |
| `SIGN_IMAGE_MIRRORED` | true | Gambar kamera tercermin (`camera.mirror(1)`): balik kiri-kanan |
| `SIGN_SPEAK_DISTANCE` | true | Sebut jarak dalam ucapan |

Model dan ambang:

| Variabel | Default | Fungsi |
|---|---:|---|
| `YOLO_CONFIDENCE` | 0.40 | Filter skor objek |
| `YOLO_IOU` | 0.45 | NMS kotak duplikat |
| `YOLO_IMAGE_SIZE` | 640 | Resolusi input YOLO |
| `YOLO_MODEL_PATH` | model/yolov5n.pt | Bobot objek |
| `QUICK_OBJECT_FRAMES` | 3 | 1–3 frame; 1 tanpa konfirmasi temporal |
| `AI_CPU_THREADS` | 2 | Thread PyTorch |
| `OBSTACLE_WARNING_CM` | 50 | Ambang perhatian sensor |
| `OBSTACLE_LARGE_AREA` | 0.08 | Rasio luas untuk objek besar |
| `SCENE_CONFIDENCE` / `SCENE_MIN_AREA_RATIO` | 0.65 / 0.015 | Segmentasi |
| `SIGN_MIN_CONFIDENCE` | 0.35 | Ambang minimum deteksi rambu |
| `SIGN_RELIABLE_CONFIDENCE` | 0.50 | Ambang agar rambu diucapkan |
| `SIGN_MODEL_PATH` | model/rambu_yolo.pt | Model rambu |
| `ESP32CAM_URL` | — | Alamat ESP32-CAM, mis. `http://192.168.1.60` |
| `ESP32_SHARED_TOKEN` | — | Samakan dengan `ESP32_TOKEN` di `secrets.h` |

## Pengujian

```bash
pip install pytest
python -m pytest
node tests/test_frontend.cjs && node tests/test_esp32_frontend.cjs
python tools/check_quick_commands.py          # smoke test inferensi nyata (butuh torch)
python tools/check_esp32_event.py --event object_detection
```

Uji lapangan yang wajib: ukur ulang `SIGN_REF_PIXELS` (rambu nyata di jarak diketahui), uji rambu di
kiri/kanan untuk memastikan `SIGN_IMAGE_MIRRORED`, dan ukur latensi total sampai suara terdengar di TWS.

Jalankan **satu proses server**: session/audio-job in-memory, belum cocok multi-worker.
