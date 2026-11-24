"use strict";

function getOrCreateSessionId() {
  let id = localStorage.getItem("spidersense_session_id");
  if (!id) {
    id = crypto.randomUUID ? crypto.randomUUID() :
      "sess-" + Date.now() + "-" + Math.random().toString(16).slice(2);
    localStorage.setItem("spidersense_session_id", id);
  }
  return id;
}

function newRequestId() {
  return crypto.randomUUID ? crypto.randomUUID() :
    "req-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}

const SESSION_ID = getOrCreateSessionId();
const els = {
  connStatus: document.getElementById("connStatus"),
  deviceStatus: document.getElementById("deviceStatus"),
  deviceIdInput: document.getElementById("deviceIdInput"),
  pairBtn: document.getElementById("pairBtn"),
  preview: document.getElementById("preview"),
  overlay: document.getElementById("overlayCanvas"),
  snapshotCanvas: document.getElementById("snapshotCanvas"),
  talkBtn: document.getElementById("talkBtn"),
  talkStatus: document.getElementById("talkStatus"),
  detectStatus: document.getElementById("detectStatus"),
  resultText: document.getElementById("resultText"),
  resultAudio: document.getElementById("resultAudio"),
  replayBtn: document.getElementById("replayBtn"),
  esp32Status: document.getElementById("esp32Status"),
  esp32Log: document.getElementById("esp32Log"),
  serverPreview: document.getElementById("serverPreview"),
  retryCameraBtn: document.getElementById("retryCameraBtn"),
  resultWarnings: document.getElementById("resultWarnings"),
  legacyVoice: document.getElementById("legacyVoice"),
};

let ws = null;
let mediaStream = null;
let audioStream = null;
let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let stopRequested = false;
let recognitionTimeoutId = null;
let requestBusy = false;
let lastAudioUrl = null;
let pairedDeviceId = null;
let lastSpokenStatus = "";
let lastSpokenStatusAt = 0;
let clientConfig = { camera_source: "browser", width: 1280, height: 720, object_frames: 3,
  burst_gap_ms: 100, max_snapshot_width: 1920 };
let cameraBusy = false;
let quickBusy = false;
let voiceCaptureBusy = false;
let audioGeneration = 0;
let disposed = false;
let esp32Source = null;
let esp32RetryMs = 2000;
let esp32ReconnectTimer = null;
const ESP32_MODES = ["objek", "rambu"];
const quickButtons = Array.from(document.querySelectorAll(".quick"));
const cameraSelect = document.getElementById("cameraSelect");
const quickStatus = document.getElementById("quickStatus");
const resultCanvas = document.getElementById("resultCanvas");

function setStatus(el, text) {
  if (el) el.textContent = text;
}

/* Kunci kontrol selama satu perintah berjalan. Tombol yang tidak ditampilkan
 * di UI cukup dilewati; alur runQuick() sendiri tidak berubah. */
function setControlsBusy(busy) {
  quickButtons.forEach((b) => { b.disabled = busy; });
  if (cameraSelect) cameraSelect.disabled = busy;
  if (els.retryCameraBtn) els.retryCameraBtn.disabled = busy;
}

function speak(text) {
  if (!text) return;
  try {
    const utter = new SpeechSynthesisUtterance(text);
    utter.lang = "id-ID";
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utter);
  } catch (e) { /* fallback is optional */ }
}

function speakStatusOnce(text, cooldownMs = 3500) {
  const now = Date.now();
  if (text === lastSpokenStatus && now - lastSpokenStatusAt < cooldownMs) return;
  lastSpokenStatus = text;
  lastSpokenStatusAt = now;
  speak(text);
}

/* Camera preview is local only. There is no detection polling. */
async function initCamera() {
  if (cameraBusy || quickBusy || requestBusy || voiceCaptureBusy || disposed) return;
  if (clientConfig.camera_source === "server") {
    els.preview.hidden = true;
    if (els.serverPreview) {
      els.serverPreview.hidden = false;
      els.serverPreview.src = "/video_feed";
    }
    if (cameraSelect) cameraSelect.hidden = true;
    setStatus(els.detectStatus, "Webcam dibuka oleh server saja.");
    return;
  }
  cameraBusy = true;
  try {
    if (mediaStream) mediaStream.getTracks().forEach((track) => track.stop());
    mediaStream = null;
    if (!navigator.mediaDevices || !window.isSecureContext) {
      throw new Error("Kamera memerlukan HTTPS atau localhost.");
    }
    mediaStream = await navigator.mediaDevices.getUserMedia({
      video: {
        ...(cameraSelect.value ? { deviceId: { exact: cameraSelect.value } } : {}),
        facingMode: { ideal: "environment" },
        width: { ideal: clientConfig.width },
        height: { ideal: clientConfig.height },
      },
      audio: false,
    });
    els.preview.srcObject = mediaStream;
    await els.preview.play();
    const activeId = mediaStream.getVideoTracks()[0].getSettings().deviceId;
    const devices = await navigator.mediaDevices.enumerateDevices();
    cameraSelect.replaceChildren();
    devices.filter((d) => d.kind === "videoinput").forEach((d, index) => {
      const option = new Option(d.label || `Kamera ${index+1}`, d.deviceId);
      option.selected = d.deviceId === activeId;
      cameraSelect.add(option);
    });
    mediaStream.getVideoTracks()[0].onended = () => setStatus(els.detectStatus,
      "Kamera terputus. Tekan Hubungkan ulang kamera.");
    setStatus(els.detectStatus, "Kamera siap. AI hanya memproses gambar saat diperlukan.");
  } catch (e) {
    setStatus(els.detectStatus, `Kamera tidak tersedia: ${e.message}. Periksa izin dan aplikasi lain yang memakai kamera.`);
  } finally { cameraBusy = false; }
}

async function ensureMicrophone() {
  if (audioStream && audioStream.getAudioTracks().some((t) => t.readyState === "live")) {
    return audioStream;
  }
  audioStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
    video: false,
  });
  return audioStream;
}

function bestAudioMimeType() {
  if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4",
  ];
  return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

function fileNameForAudio(blob) {
  const type = (blob && blob.type || "").toLowerCase();
  if (type.includes("ogg")) return "speech.ogg";
  if (type.includes("mp4")) return "speech.m4a";
  if (type.includes("mpeg") || type.includes("mp3")) return "speech.mp3";
  return "speech.webm";
}

async function captureSnapshotBlob(highQuality = true) {
  if (!mediaStream || !mediaStream.getVideoTracks().some((t) => t.readyState === "live") || !els.preview.videoWidth || els.preview.readyState < 2) return null;
  // Wait for a decoded camera frame, not only a display refresh. Timeout covers
  // unplugged cameras and background tabs, which can stop frame callbacks.
  await new Promise((resolve, reject) => {
    let callbackId;
    const timeout = setTimeout(() => {
      if (callbackId && els.preview.cancelVideoFrameCallback) els.preview.cancelVideoFrameCallback(callbackId);
      reject(new Error("Kamera tidak menghasilkan frame baru"));
    }, 2500);
    const done = () => { clearTimeout(timeout); resolve(); };
    if (els.preview.requestVideoFrameCallback) callbackId = els.preview.requestVideoFrameCallback(done);
    else setTimeout(done, 100);
  });
  return new Promise((resolve) => {
    const video = els.preview;
    const canvas = els.snapshotCanvas;
    // Voice is transcribed on the server, so the client does not yet know if
    // this is traffic-sign detection ("rambu"). Capture one reasonably detailed frame;
    // the server decodes and processes it only if the routed command needs vision.
    const maxW = highQuality ? clientConfig.max_snapshot_width : 960;
    const scale = Math.min(1, maxW / video.videoWidth);
    const w = Math.max(1, Math.round(video.videoWidth * scale));
    const h = Math.max(1, Math.round(video.videoHeight * scale));
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d", { alpha: false });
    ctx.drawImage(video, 0, 0, w, h);
    canvas.toBlob((blob) => resolve(blob), "image/jpeg", highQuality ? 0.94 : 0.84);
  });
}

function clearOverlay() {
  const ctx = els.overlay.getContext("2d");
  ctx.clearRect(0, 0, els.overlay.width, els.overlay.height);
}

function drawOverlay(data) {
  const canvas = els.overlay;
  const video = els.preview;
  const dw = video.clientWidth;
  const dh = video.clientHeight;
  if (!dw || !dh) return;

  canvas.width = dw;
  canvas.height = dh;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, dw, dh);

  const fw = data.frame_width || video.videoWidth || dw;
  const fh = data.frame_height || video.videoHeight || dh;
  const sx = dw / fw;
  const sy = dh / fh;
  ctx.lineWidth = 2;
  ctx.font = "14px sans-serif";
  ctx.textBaseline = "top";

  function drawBox(box, label, color) {
    if (!Array.isArray(box) || box.length < 4) return;
    const [x, y, w, h] = box;
    const rx = x * sx, ry = y * sy, rw = w * sx, rh = h * sy;
    ctx.strokeStyle = color;
    ctx.strokeRect(rx, ry, rw, rh);
    const textW = Math.min(Math.max(rw, 90), ctx.measureText(label).width + 8);
    const ty = Math.max(0, ry - 18);
    ctx.fillStyle = color;
    ctx.fillRect(rx, ty, textW, 18);
    ctx.fillStyle = "#000";
    ctx.fillText(label, rx + 4, ty + 1);
  }

  (data.detected_objects || []).forEach((o) =>
    drawBox(o.box, o.label_id || o.label, "#00e5ff"));
  (data.faces || []).forEach((f) => drawBox(f.box, "Face", "#ffcc00"));
  (data.sign_boxes || []).forEach((t) => drawBox(t.box, t.label, "#00ff66"));
}

function connectWebSocket() {
  if (disposed || clientConfig.camera_source === "server") return;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen = () => {
    setStatus(els.connStatus, "Terhubung ke server.");
    ws.send(JSON.stringify({ type: "register", role: "web", session_id: SESSION_ID }));
  };
  ws.onclose = () => {
    setStatus(els.connStatus, "Terputus dari server. Mencoba lagi...");
    if (!disposed) setTimeout(connectWebSocket, 2000);
  };
  ws.onerror = () => setStatus(els.connStatus, "Server tidak dapat terhubung.");
  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch (e) { return; }
    switch (msg.type) {
      case "registered":
        setStatus(els.connStatus, "Terhubung ke server.");
        break;
      case "paired":
        pairedDeviceId = msg.device_id || pairedDeviceId;
        setStatus(els.deviceStatus, `Perangkat ${msg.device_id} terpasang.`);
        speakStatusOnce("Perangkat terpasang.");
        break;
      case "start_recording":
        startRecording();
        break;
      case "stop_recording":
        stopRecording();
        break;
      case "toggle": {
        const text = msg.state === "on" ? "Sistem aktif." : "Sistem standby.";
        setStatus(els.deviceStatus, text);
        speakStatusOnce(text);
        break;
      }
      case "status":
        setStatus(els.resultText, msg.message);
        speakStatusOnce(msg.message);
        break;
      default:
        break;
    }
  };
}

els.pairBtn?.addEventListener("click", () => {
  const deviceId = els.deviceIdInput.value.trim();
  if (!deviceId) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    setStatus(els.deviceStatus, "Server tidak dapat terhubung.");
    return;
  }
  ws.send(JSON.stringify({ type: "pair_device", device_id: deviceId }));
});

async function startRecording() {
  if (isRecording || requestBusy || quickBusy || clientConfig.camera_source === "server") return;
  if (!window.MediaRecorder) {
    const msg = "Browser ini tidak mendukung perekaman audio MediaRecorder.";
    setStatus(els.talkStatus, msg);
    speakStatusOnce(msg);
    return;
  }

  isRecording = true;
  stopRequested = false;
  audioChunks = [];
  els.talkBtn?.setAttribute("aria-pressed", "true");
  setStatus(els.talkStatus, "Mendengarkan...");

  try {
    const stream = await ensureMicrophone();
    if (stopRequested) {
      isRecording = false;
      els.talkBtn?.setAttribute("aria-pressed", "false");
      return;
    }

    const mimeType = bestAudioMimeType();
    mediaRecorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) audioChunks.push(event.data);
    };
    mediaRecorder.onerror = (event) => {
      console.error("MediaRecorder error", event.error || event);
      setStatus(els.talkStatus, "Perekaman suara gagal. Coba lagi.");
    };
    mediaRecorder.onstop = async () => {
      clearRecordingTimeout();
      isRecording = false;
      els.talkBtn?.setAttribute("aria-pressed", "false");
      const type = mediaRecorder && mediaRecorder.mimeType ? mediaRecorder.mimeType : (audioChunks[0] && audioChunks[0].type) || "audio/webm";
      const audioBlob = new Blob(audioChunks, { type });
      audioChunks = [];
      if (audioBlob.size < 256) {
        setStatus(els.talkStatus, "Suara terlalu singkat. Coba lagi.");
        return;
      }
      setStatus(els.talkStatus, "Memproses suara...");
      // Capture exactly one camera frame per spoken prompt. It is intentionally
      // NOT processed by the server for ordinary conversation.
      voiceCaptureBusy = true;
      try {
        const snapshotBlob = await captureSnapshotBlob(true);
        await sendToServer({ audioBlob, snapshotBlob });
      } catch (e) {
        setStatus(els.talkStatus, e.message);
      } finally { voiceCaptureBusy = false; }
    };

    mediaRecorder.start(100);
    recognitionTimeoutId = setTimeout(() => stopRecording(), 15000);
  } catch (e) {
    isRecording = false;
    els.talkBtn?.setAttribute("aria-pressed", "false");
    const msg = "Mikrofon tidak dapat digunakan. Periksa izin browser/HTTPS.";
    setStatus(els.talkStatus, msg);
    speakStatusOnce(msg);
  }
}

function clearRecordingTimeout() {
  if (recognitionTimeoutId) {
    clearTimeout(recognitionTimeoutId);
    recognitionTimeoutId = null;
  }
}

function stopRecording() {
  stopRequested = true;
  clearRecordingTimeout();
  if (mediaRecorder && mediaRecorder.state === "recording") {
    try { mediaRecorder.stop(); } catch (e) { /* noop */ }
  }
}

els.talkBtn?.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  startRecording();
});
["pointerup", "pointercancel", "pointerleave"].forEach((evt) =>
  els.talkBtn?.addEventListener(evt, (e) => {
    e.preventDefault();
    stopRecording();
  })
);

function playResultAudio(url, fallbackText = "") {
  if (!url) {
    if (fallbackText) speak(fallbackText);
    return;
  }
  try { els.resultAudio.pause(); } catch (e) { /* noop */ }
  lastAudioUrl = url;
  els.resultAudio.src = url;
  els.resultAudio.currentTime = 0;
  const playPromise = els.resultAudio.play();
  if (playPromise && typeof playPromise.catch === "function") {
    playPromise.catch(() => {
      if (fallbackText) speak(fallbackText);
    });
  }
}

els.replayBtn?.addEventListener("click", () => {
  if (!lastAudioUrl) { speak(els.resultText.textContent); return; }
  els.resultAudio.src = lastAudioUrl;
  try { els.resultAudio.pause(); } catch (e) { /* noop */ }
  els.resultAudio.currentTime = 0;
  els.resultAudio.play().catch(() => {
    if (els.resultText && els.resultText.textContent) speak(els.resultText.textContent);
  });
});

async function sendToServer({ transcript = "", audioBlob = null, snapshotBlob = null } = {}) {
  if (requestBusy || quickBusy) return;
  stopOutput();
  requestBusy = true;
  const requestId = newRequestId();

  try {
    clearOverlay();
    const form = new FormData();
    form.append("session_id", SESSION_ID);
    form.append("request_id", requestId);
    if (transcript) form.append("transcript", transcript);
    if (audioBlob) form.append("audio", audioBlob, fileNameForAudio(audioBlob));
    if (snapshotBlob) form.append("image", snapshotBlob, "snapshot.jpg");

    const resp = await fetch("/api/upload", { method: "POST", body: form });
    const data = await resp.json().catch(() => ({}));

    if (data.status === "ignored" || data.duplicate_suppressed) {
      if (data.result) setStatus(els.resultText, data.result);
      setStatus(els.talkStatus, "Siap mendengarkan.");
      return;
    }

    if (!resp.ok || (data.status !== "success" && data.status !== "partial")) {
      const msg = data.message || "Maaf, sistem AI sedang tidak tersedia.";
      setStatus(els.resultText, msg);
      setStatus(els.talkStatus, "Siap mendengarkan.");
      speakStatusOnce(msg);
      return;
    }

    setStatus(els.resultText, data.result);
    setStatus(els.talkStatus, "Siap mendengarkan.");
    drawOverlay(data);

    if (data.audio_url) {
      const url = data.audio_url + "?rid=" + encodeURIComponent(data.request_id || requestId);
      lastAudioUrl = url;
      if (data.audio_output !== "esp32") {
        playResultAudio(url, data.result);
      }
    } else if (data.audio_output !== "esp32") {
      // Last-resort local browser TTS: user still hears an answer if the online
      // MP3 provider is temporarily unavailable.
      speak(data.result);
    }
  } catch (e) {
    setStatus(els.resultText, "Server tidak dapat terhubung.");
    setStatus(els.talkStatus, "Siap mendengarkan.");
    speakStatusOnce("Server tidak dapat terhubung.");
  } finally {
    requestBusy = false;
    isRecording = false;
    els.talkBtn?.setAttribute("aria-pressed", "false");
  }
}

function stopOutput() {
  audioGeneration += 1;
  els.resultAudio.pause();
  window.speechSynthesis?.cancel();
  lastAudioUrl = null;
  return audioGeneration;
}

async function pollQuickAudio(url, text, generation) {
  // Audio is independent: never delay visual results or lock another command.
  for (let i = 0; i < 60 && generation === audioGeneration && !disposed; i++) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10000);
      let response;
      try { response = await fetch(url, {signal: controller.signal}); }
      finally { clearTimeout(timer); }
      const data = await response.json();
      if (generation !== audioGeneration) return;
      if (data.status === "ready") {
        lastAudioUrl = data.audio_url;
        if (data.audio_output === "browser") playResultAudio(data.audio_url, text);
        else els.resultAudio.src = data.audio_url;
        return;
      }
      if (data.status === "superseded") return;
      if (data.status !== "pending") { speak(text); return; }
    } catch (e) { if (generation === audioGeneration) speak(text); return; }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  if (generation === audioGeneration) setStatus(quickStatus, "Hasil siap. Audio masih diproses; gunakan Dengar ulang jawaban.");
}

async function drawQuickSnapshot(blob, data) {
  if (!blob) { resultCanvas.hidden = true; return; }
  const bitmap = await createImageBitmap(blob);
  const scale = Math.min(1, 960/bitmap.width);
  resultCanvas.width = Math.round(bitmap.width*scale);
  resultCanvas.height = Math.round(bitmap.height*scale);
  const ctx = resultCanvas.getContext("2d");
  ctx.drawImage(bitmap, 0, 0, resultCanvas.width, resultCanvas.height);
  bitmap.close();
  const sx = resultCanvas.width/data.frame_width, sy = resultCanvas.height/data.frame_height;
  ctx.font = "16px sans-serif"; ctx.lineWidth = 2;
  const boxes = [...(data.detected_objects || []).map((b) => ({...b, caption: b.label_id || b.label})),
    ...(data.sign_boxes || []).map((b) => ({...b, caption: b.label}))];
  boxes.forEach((b) => {
    const [x, y, w, h] = b.box;
    ctx.strokeStyle = b.possible_obstacle ? "#ffcc00" : "#00ffcc";
    ctx.strokeRect(x*sx, y*sy, w*sx, h*sy);
    const ty = Math.max(18, y*sy);
    ctx.fillStyle = "#000"; ctx.fillRect(x*sx, ty-18, ctx.measureText(b.caption).width+8, 20);
    ctx.fillStyle = "#fff"; ctx.fillText(b.caption, x*sx+4, ty-2);
  });
  resultCanvas.hidden = false;
}

async function runQuick(mode) {
  if (quickBusy || requestBusy || isRecording || cameraBusy || voiceCaptureBusy) return;
  quickBusy = true;
  const generation = stopOutput();
  setControlsBusy(true);
  setStatus(quickStatus, "Mengambil gambar dan memproses. Pemanggilan pertama memuat model...");
  setStatus(els.resultWarnings, "");
  resultCanvas.hidden = true;
  setStatus(els.resultText, "Menunggu hasil baru...");
  clearOverlay();
  try {
    const form = new FormData();
    form.append("session_id", SESSION_ID); form.append("request_id", newRequestId()); form.append("mode", mode);
    let lastBlob = null;
    if (clientConfig.camera_source === "browser") {
      const count = mode === "objek" ? clientConfig.object_frames : 1;
      for (let i = 0; i < count; i++) {
        lastBlob = await captureSnapshotBlob(true);
        if (!lastBlob) throw new Error("Kamera belum siap; hubungkan ulang kamera.");
        form.append("image", lastBlob, `snapshot-${i}.jpg`);
        if (i+1 < count) await new Promise((resolve) => setTimeout(resolve, clientConfig.burst_gap_ms));
      }
      form.append("camera_id", mediaStream.getVideoTracks()[0].getSettings().deviceId || "browser");
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 120000);
    let response;
    try { response = await fetch("/api/quick", { method: "POST", body: form, signal: controller.signal }); }
    finally { clearTimeout(timer); }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || `Server error ${response.status}`);
    setStatus(els.resultText, data.result);
    setStatus(els.resultWarnings, (data.warnings || []).join("\n"));
    setStatus(quickStatus, `Selesai (${data.processing_ms} ms pemrosesan). Hasil berasal dari snapshot, bukan deteksi langsung.`);
    try {
      if (!lastBlob && data.image_url?.startsWith("data:image/jpeg;base64,")) {
        lastBlob = await (await fetch(data.image_url)).blob();
      }
      await drawQuickSnapshot(lastBlob, data);
    } catch (e) { /* result text remains available */ }
    if (data.audio_job) pollQuickAudio(data.audio_job, data.result, generation);
    else speak(data.result);
  } catch (e) {
    const message = e.name === "AbortError" ? "Server terlalu lama merespons. Proses server mungkin masih berjalan; tunggu sebelum mencoba lagi." : e.message;
    setStatus(els.resultText, message); setStatus(quickStatus, "Tidak berhasil. Perbaiki penyebab lalu coba lagi.");
    speakStatusOnce(message);
  } finally {
    quickBusy = false;
    setControlsBusy(false);
  }
}

/* ---------------------------------------------------------------------------
 * Monitor tombol fisik ESP32 (OPSIONAL).
 *
 * Sejak migrasi ke ESP32-CAM, tombol fisik memicu deteksi LANGSUNG di backend
 * (ESP32 -> backend -> ESP32-CAM -> AI -> speaker ESP32). Halaman ini tidak lagi
 * menjalankan deteksi atas nama tombol fisik; ia hanya menampilkan informasi yang
 * dikirim backend lewat Server-Sent Events: quick_started, quick_rejected,
 * quick_result. Menjalankan runQuick() di sini akan menggandakan deteksi.
 * ------------------------------------------------------------------------ */
function setEsp32Status(text) {
  setStatus(els.esp32Status, text);
}

function appendEsp32Log(text) {
  if (!els.esp32Log) return;
  const stamp = new Date().toLocaleTimeString("id-ID");
  const lines = (els.esp32Log.textContent || "").split("\n").filter(Boolean);
  lines.unshift(`${stamp} — ${text}`);
  els.esp32Log.textContent = lines.slice(0, 6).join("\n");
}

function handleEsp32Event(msg) {
  if (!msg || !msg.type) return;
  const label = msg.label || msg.mode || "";
  if (msg.type === "quick_started") {
    setEsp32Status(`${label} dimulai (diproses backend)`);
    appendEsp32Log(`${label} dimulai`);
    setStatus(quickStatus, "Deteksi dimulai. Backend memotret lewat ESP32-CAM dan memproses...");
  } else if (msg.type === "quick_rejected") {
    const why = msg.reason || "tidak dimulai";
    setEsp32Status(`${label} tidak dijalankan: ${why}`);
    appendEsp32Log(`${label} ditolak: ${why}`);
  } else if (msg.type === "quick_result") {
    setStatus(els.resultText, msg.result || "");
    setStatus(quickStatus, msg.http_status === 200
      ? `Selesai (${msg.processing_ms} ms). Suara diputar di speaker ESP32.`
      : "Perintah tombol fisik gagal.");
  }
}

function scheduleEsp32Reconnect() {
  if (disposed || esp32ReconnectTimer) return;
  esp32ReconnectTimer = setTimeout(() => {
    esp32ReconnectTimer = null;
    connectEsp32Stream();
  }, esp32RetryMs);
  esp32RetryMs = Math.min(esp32RetryMs * 2, 30000);
}

function connectEsp32Stream() {
  if (disposed) return;
  if (typeof EventSource === "undefined") {
    setEsp32Status("Browser ini tidak mendukung tombol fisik ESP32. Gunakan tombol layar.");
    return;
  }
  if (esp32Source) {
    try { esp32Source.close(); } catch (e) { /* noop */ }
    esp32Source = null;
  }
  setEsp32Status("Menghubungkan ke jalur tombol fisik...");
  try {
    esp32Source = new EventSource(`/api/esp32/stream?session_id=${encodeURIComponent(SESSION_ID)}`);
  } catch (e) {
    setEsp32Status("Jalur tombol fisik tidak dapat dibuka. Tombol layar tetap bisa dipakai.");
    scheduleEsp32Reconnect();
    return;
  }
  esp32Source.onopen = () => {
    esp32RetryMs = 2000;
    setEsp32Status("Monitor tersambung. Menunggu deteksi dari ESP32.");
  };
  esp32Source.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch (e) { return; }
    if (msg.type === "ready") {
      setEsp32Status("Monitor siap: deteksi objek (sensor) dan rambu (terus-menerus).");
      return;
    }
    handleEsp32Event(msg);
  };
  esp32Source.onerror = () => {
    if (esp32Source) {
      try { esp32Source.close(); } catch (e) { /* noop */ }
      esp32Source = null;
    }
    // Kehilangan stream TIDAK menonaktifkan apa pun: tombol UI tetap jalan.
    setEsp32Status("Jalur tombol fisik terputus. Tombol layar tetap bisa dipakai.");
    scheduleEsp32Reconnect();
  };
}

quickButtons.forEach((b) => b.addEventListener("click", () => runQuick(b.dataset.mode)));
cameraSelect?.addEventListener("change", () => { clearOverlay(); initCamera(); });
els.retryCameraBtn?.addEventListener("click", initCamera);
window.addEventListener("pagehide", () => {
  disposed = true; stopOutput();
  if (esp32ReconnectTimer) { clearTimeout(esp32ReconnectTimer); esp32ReconnectTimer = null; }
  if (esp32Source) { try { esp32Source.close(); } catch (e) { /* noop */ } esp32Source = null; }
  if (ws) ws.close();
  if (mediaStream) mediaStream.getTracks().forEach((t) => t.stop());
  if (audioStream) audioStream.getTracks().forEach((t) => t.stop());
});
async function start() {
  try { clientConfig = {...clientConfig, ...await (await fetch("/api/client_config")).json()}; }
  catch (e) {
    setStatus(els.connStatus, "Konfigurasi server belum tersedia. Muat ulang halaman setelah server siap.");
    quickButtons.forEach((b) => { b.disabled = true; });
    return;
  }
  if (clientConfig.camera_source === "server") {
    if (els.legacyVoice) els.legacyVoice.hidden = true;
    if (els.pairBtn) els.pairBtn.disabled = true;
    setStatus(els.connStatus, "Mode webcam lokal.");
  }
  await initCamera();
  connectWebSocket();
  connectEsp32Stream();
}
/* API konsol untuk pengujian manual tombol layar. */
window.runQuick = runQuick;
window.spiderSense = {
  MODES: ESP32_MODES,
  runQuick,                                  // runQuick("objek" | "rambu")
  deteksiObjek: () => runQuick("objek"),
  deteksiRambu: () => runQuick("rambu"),
  handleEsp32Event,
  initCamera,
};

start();
