"""Konfigurasi terpusat Spider Sense.

Seluruh kredensial dan parameter dibaca dari environment variable (file
``.env`` di root project). Meng-import modul ini tidak pernah melakukan I/O
jaringan.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    base_dir: Path = BASE_DIR

    # Groq / LLM / STT
    groq_api_key: str = os.getenv("GROQ_API_KEY", "").strip()
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
    groq_stt_model: str = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo").strip()
    groq_timeout_seconds: float = float(os.getenv("GROQ_TIMEOUT_SECONDS", "20"))
    groq_max_output_tokens: int = int(os.getenv("GROQ_MAX_OUTPUT_TOKENS", "220"))
    groq_reasoning_effort: str = os.getenv("GROQ_REASONING_EFFORT", "low").strip()
    stt_language: str = os.getenv("STT_LANGUAGE", "id").strip()

    # TTS. Groq's hosted TTS currently has no Indonesian voice. The project
    # therefore uses Edge TTS and falls back to gTTS for an MP3 output that is
    # directly playable by the browser and ESP32-audioI2S.
    tts_provider: str = os.getenv("TTS_PROVIDER", "auto").strip().lower()
    tts_language: str = os.getenv("TTS_LANGUAGE", "id-ID").strip()
    tts_voice: str = os.getenv("TTS_VOICE", "id-ID-ArdiNeural").strip()
    local_audio_playback: bool = _bool("LOCAL_AUDIO_PLAYBACK", True)

    # Data services
    mongodb_uri: str = (os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "").strip()
    mongodb_tls: bool = _bool("MONGODB_TLS", True)
    mongodb_tls_ca_file: str = os.getenv("MONGODB_TLS_CA_FILE", "").strip()
    mongodb_tls_disable_ocsp: bool = _bool("MONGODB_TLS_DISABLE_OCSP_ENDPOINT_CHECK", True)
    mongodb_server_selection_timeout_ms: int = int(os.getenv("MONGODB_SERVER_SELECTION_TIMEOUT_MS", "5000"))
    mongodb_connect_timeout_ms: int = int(os.getenv("MONGODB_CONNECT_TIMEOUT_MS", "5000"))
    mongodb_socket_timeout_ms: int = int(os.getenv("MONGODB_SOCKET_TIMEOUT_MS", "10000"))
    mongodb_max_pool_size: int = int(os.getenv("MONGODB_MAX_POOL_SIZE", "10"))
    mongodb_retry_initial_seconds: float = float(os.getenv("MONGODB_RETRY_INITIAL_SECONDS", "30"))
    mongodb_retry_max_seconds: float = float(os.getenv("MONGODB_RETRY_MAX_SECONDS", "300"))
    mongodb_error_log_interval_seconds: float = float(os.getenv("MONGODB_ERROR_LOG_INTERVAL_SECONDS", "60"))
    ubidots_token: str = os.getenv("UBIDOTS_TOKEN", "").strip()
    ubidots_device_label: str = os.getenv("UBIDOTS_DEVICE_LABEL", "spider-sense").strip()

    # Kamera. ESP32-CAM adalah sumber gambar utama: backend menarik JPEG dari
    # firmware MicroPython di firmware/esp32_cam/main.py saat tombol fisik ditekan
    # (server firmware ini membalas SETIAP request dengan JPEG baru; tidak ada
    # path/token khusus — lihat camera/esp32cam.py). ``webcam`` hanya untuk uji
    # AI di meja tanpa perangkat keras (opsional).
    camera_source: str = os.getenv("CAMERA_SOURCE", "esp32cam").strip().lower()
    esp32cam_url: str = os.getenv("ESP32CAM_URL", "").strip().rstrip("/")
    esp32cam_token: str = os.getenv("ESP32CAM_TOKEN", "").strip()
    esp32cam_timeout_seconds: float = float(os.getenv("ESP32CAM_TIMEOUT_SECONDS", "6"))
    esp32cam_retries: int = int(os.getenv("ESP32CAM_RETRIES", "2"))
    esp32cam_burst_gap_ms: int = int(os.getenv("ESP32CAM_BURST_GAP_MS", "120"))
    esp32cam_best_of: int = int(os.getenv("ESP32CAM_BEST_OF", "2"))
    esp32cam_flash: bool = _bool("ESP32CAM_FLASH", False)

    # Server/session
    host: str = os.getenv("HOST", "0.0.0.0").strip()
    port: int = int(os.getenv("PORT", "5000"))
    # Halaman web lama (folder web/) hanya kanal monitor opsional. Sistem utama
    # (tombol fisik -> ESP32-CAM -> AI -> speaker ESP32) tidak membutuhkannya.
    web_ui_enabled: bool = _bool("WEB_UI_ENABLED", False)
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", "3600"))
    prompt_repeat_guard_seconds: float = float(os.getenv("PROMPT_REPEAT_GUARD_SECONDS", "3.0"))

    # Deteksi otomatis (mode biasa). Mode hemat daya tidak menjalankan keduanya.
    #
    # 1) Deteksi objek dipicu sensor ultrasonik ESP32: saat jarak benda
    #    <= OBJECT_TRIGGER_CM, backend memotret + mendeteksi objek, lalu menahan
    #    pemicu berikutnya selama OBJECT_AUTO_COOLDOWN_SECONDS (default 60 dtk) supaya
    #    benda yang tetap berada di jarak 1 m tidak memicu deteksi terus-menerus.
    object_trigger_cm: float = float(os.getenv("OBJECT_TRIGGER_CM", "100"))
    object_auto_cooldown_seconds: float = float(os.getenv("OBJECT_AUTO_COOLDOWN_SECONDS", "60"))

    # 2) Deteksi rambu berjalan terus-menerus di backend (SignWatcher), TERPISAH dari
    #    deteksi objek: kunci AI sendiri, tidak menunggu sensor, dan tidak saling
    #    memblokir. Keluaran hanya muncul saat ukuran kotak label memenuhi SIGN_MIN_PIXELS.
    sign_watch_enabled: bool = _bool("SIGN_WATCH_ENABLED", True)
    sign_watch_interval_seconds: float = float(os.getenv("SIGN_WATCH_INTERVAL_SECONDS", "1.0"))
    # Rambu yang sama baru diumumkan ulang setelah jeda ini (kecuali arah jamnya berubah jauh).
    sign_announce_cooldown_seconds: float = float(os.getenv("SIGN_ANNOUNCE_COOLDOWN_SECONDS", "20"))
    # Rambu harus terlihat di N siklus berurutan sebelum diumumkan (menekan deteksi palsu).
    sign_confirm_frames: int = int(os.getenv("SIGN_CONFIRM_FRAMES", "2"))
    # Kalibrasi jarak: sisi terpanjang kotak label = SIGN_REF_PIXELS piksel saat rambu
    # berjarak SIGN_REF_DISTANCE_M meter, diukur pada gambar selebar SIGN_REF_FRAME_WIDTH.
    sign_ref_pixels: float = float(os.getenv("SIGN_REF_PIXELS", "85"))
    sign_ref_distance_m: float = float(os.getenv("SIGN_REF_DISTANCE_M", "2.0"))
    sign_ref_frame_width: int = int(os.getenv("SIGN_REF_FRAME_WIDTH", "640"))
    # Rambu dideteksi terus, tetapi BARU DIUCAPKAN bila sisi terpanjang kotak labelnya
    # >= SIGN_MIN_PIXELS piksel (dinormalkan ke lebar SIGN_REF_FRAME_WIDTH, sama seperti
    # kalibrasi jarak). Default = SIGN_REF_PIXELS, yaitu rambu sudah sedekat titik
    # kalibrasi (85 px = 2 m). Naikkan untuk hanya mengumumkan rambu yang lebih dekat,
    # turunkan untuk rambu yang lebih jauh. 0 = tanpa batas ukuran.
    sign_min_pixels: float = float(os.getenv("SIGN_MIN_PIXELS", os.getenv("SIGN_REF_PIXELS", "85")))
    # Firmware ESP32-CAM memakai camera.mirror(1): gambar tercermin kiri-kanan,
    # jadi arah jam harus dibalik. Set false bila mirror dimatikan.
    sign_image_mirrored: bool = _bool("SIGN_IMAGE_MIRRORED", True)
    sign_speak_distance: bool = _bool("SIGN_SPEAK_DISTANCE", True)

    # ESP32 mengirim event lewat WebSocket /ws (Wi-Fi): daftar, perubahan mode,
    # dan sampel jarak ultrasonik. POST /api/esp32/event dan serial USB tetap ada
    # untuk uji manual. Backend langsung menjalankan deteksi (tanpa browser);
    # SSE /api/esp32/stream hanya memberi tahu monitor opsional.
    esp32_shared_token: str = os.getenv("ESP32_SHARED_TOKEN", "").strip()
    # Bila kirim audio ke TWS (lewat ESP32) gagal: true = putar di speaker laptop sebagai cadangan,
    # false (default) = tetap senyap di laptop dan hanya bunyikan beep di perangkat. Pengguna
    # memakai TWS, jadi suara yang tiba-tiba keluar dari laptop justru membingungkan.
    esp32_audio_local_fallback: bool = _bool("ESP32_AUDIO_LOCAL_FALLBACK", False)
    esp32_event_cooldown_ms: int = int(os.getenv("ESP32_EVENT_COOLDOWN_MS", "700"))
    esp32_max_stream_clients: int = int(os.getenv("ESP32_MAX_STREAM_CLIENTS", "8"))
    esp32_sse_heartbeat_seconds: int = int(os.getenv("ESP32_SSE_HEARTBEAT_SECONDS", "15"))


settings = Settings()