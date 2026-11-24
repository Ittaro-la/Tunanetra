"""Jarak dan arah jarum jam rambu dari kotak label YOLO.

Cara kerja (sesuai permintaan):

* Ukuran kotak label ditentukan lewat satu titik kalibrasi, mis. 85 piksel
  = 2 meter (``SIGN_REF_PIXELS`` / ``SIGN_REF_DISTANCE_M`` pada gambar selebar
  ``SIGN_REF_FRAME_WIDTH``). Model pinhole: jarak berbanding terbalik dengan
  ukuran kotak, jadi ``jarak = jarak_ref * piksel_ref / piksel_kotak``.
* Arah jam dihitung dari selisih titik pusat kamera (tengah gambar) ke titik
  pusat kotak rambu. Jam 12 = lurus ke atas, jam 3 = kanan, jam 6 = bawah,
  jam 9 = kiri. Karena skala meter-per-piksel sama di sumbu x dan y, sudutnya
  tidak bergantung pada jarak; jarak dipakai untuk laporan, bukan untuk sudut.

Modul ini murni hitungan (tanpa I/O, tanpa model) supaya mudah diuji.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Label hasil heuristik (bukan kotak YOLO): ukurannya tidak mewakili jarak.
NO_DISTANCE_LABELS = {"zebra cross"}


@dataclass(frozen=True)
class SignGeometry:
    ref_pixels: float = 85.0
    ref_distance_m: float = 2.0
    ref_frame_width: int = 640
    mirrored: bool = True
    center_deadzone: float = 0.04   # pecahan lebar gambar; di dalamnya = "tepat di depan"

    def distance_m(self, box, frame_width):
        """Perkiraan jarak (meter) dari sisi terpanjang kotak ``[x, y, w, h]``; ``None`` bila tak valid."""
        try:
            longest = float(max(box[2], box[3]))
            width = float(frame_width)
        except (TypeError, ValueError, IndexError):
            return None
        if longest <= 0 or width <= 0 or self.ref_pixels <= 0 or self.ref_frame_width <= 0:
            return None
        # Samakan skala piksel dengan resolusi saat kalibrasi.
        normalized = longest * (self.ref_frame_width / width)
        distance = self.ref_distance_m * self.ref_pixels / normalized
        return round(distance, 1) if math.isfinite(distance) else None

    def clock_hour(self, box, frame_width, frame_height):
        """Arah jam 1..12 dari pusat gambar ke pusat kotak; ``None`` bila tepat di depan."""
        x, y, w, h = [float(v) for v in box]
        dx = (x + w / 2.0) - frame_width / 2.0
        up = frame_height / 2.0 - (y + h / 2.0)        # ke atas = positif
        if self.mirrored:
            dx = -dx
        if math.hypot(dx, up) < self.center_deadzone * frame_width:
            return None
        angle = math.degrees(math.atan2(dx, up)) % 360.0   # 0 = jam 12, searah jarum jam
        hour = int(round(angle / 30.0)) % 12
        return 12 if hour == 0 else hour

    def describe(self, detection, frame_width, frame_height):
        """Tambahkan ``clock_hour``, ``distance_m`` dan teks arah ke satu deteksi."""
        item = dict(detection)
        label = str(item.get("label", "Rambu"))
        hour = self.clock_hour(item["box"], frame_width, frame_height)
        distance = None if label.strip().lower() in NO_DISTANCE_LABELS else \
            self.distance_m(item["box"], frame_width)
        item.update(clock_hour=hour, distance_m=distance,
                    direction_text="tepat di depan" if hour is None else f"arah jam {hour}")
        return item


def circular_hour_gap(a, b):
    """Selisih terkecil antar dua jam (melingkar, 0..6); ``None`` bila salah satunya tak ada."""
    if a is None or b is None:
        return None
    gap = abs(int(a) - int(b)) % 12
    return min(gap, 12 - gap)


def sentence(item, speak_distance=True):
    """Satu kalimat ucapan: ``<hasil_deteksi> berada pada arah jam N, sekitar X meter``."""
    if item.get("clock_hour") is None:
        text = f"{item['label']} berada tepat di depan"
    else:
        text = f"{item['label']} berada pada arah jam {item['clock_hour']}"
    distance = item.get("distance_m")
    if speak_distance and distance is not None:
        shown = f"{distance:.1f}".rstrip("0").rstrip(".").replace(".", " koma ")
        text += f", sekitar {shown} meter"
    return text + "."
