// Monitor opsional: tombol fisik ESP32 dijalankan LANGSUNG oleh backend, jadi event SSE
// TIDAK BOLEH memicu runQuick (akan menggandakan deteksi). Halaman hanya menampilkan
// status/hasil, dan tombol layar tetap berfungsi saat jalur ESP32 putus.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const elements = new Map();
let uploads = [], streams = [];
function element(id) {
  if (elements.has(id)) return elements.get(id);
  const el = { id, value: '', hidden: false, disabled: false, textContent: '', dataset: {},
    videoWidth: 640, videoHeight: 480, readyState: 4, width: 640, height: 480,
    listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; },
    setAttribute() {}, pause() {}, play: async () => {}, focus() { this.focused = true; },
    requestVideoFrameCallback(fn) { setTimeout(fn, 1); return 1; }, cancelVideoFrameCallback() {},
    replaceChildren() {}, add() {},
    getContext() { return { drawImage() {}, clearRect() {}, strokeRect() {}, fillRect() {}, fillText() {},
      measureText() { return {width: 30}; } }; },
    toBlob(fn) { fn(new Blob(['jpeg'], {type: 'image/jpeg'})); },
  };
  elements.set(id, el); return el;
}
const buttons = ['objek', 'rambu'].map(mode => { const b=element(mode); b.dataset.mode=mode; return b; });
const track = {readyState: 'live', stop() {}, getSettings() { return { deviceId: 'camera-A' }; }};
const stream = { getVideoTracks: () => [track], getTracks: () => [track] };

// EventSource palsu: menyimpan instance supaya test bisa mengirim pesan.
class FakeEventSource {
  constructor(url) { this.url = url; this.onopen = null; this.onmessage = null; this.onerror = null;
    this.closed = false; streams.push(this); setTimeout(() => this.onopen && this.onopen(), 1); }
  emit(obj) { if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) }); }
  close() { this.closed = true; }
}

const windowListeners = {};
const context = vm.createContext({
  console, setTimeout, clearTimeout, Blob, FormData, AbortController, crypto: require('node:crypto').webcrypto,
  Option: function(label, value) { this.label=label; this.value=value; },
  localStorage: {getItem: () => 'session-test', setItem() {}},
  document: {getElementById: element, querySelectorAll: () => buttons},
  navigator: {mediaDevices: {getUserMedia: async () => stream, enumerateDevices: async () => []}},
  window: {isSecureContext: true, speechSynthesis: {cancel() {}, speak() {}},
    addEventListener(type, fn) {windowListeners[type]=fn;}},
  SpeechSynthesisUtterance: function(text) {this.text=text;},
  WebSocket: class {static OPEN=1; close() {}}, location: {protocol:'http:', host:'localhost'},
  EventSource: FakeEventSource,
  createImageBitmap: async () => ({width:640, height:480, close() {}}),
  fetch: async (url, opts) => {
    if (url === '/api/client_config') return {json: async () => ({camera_source:'browser', burst_gap_ms:1})};
    uploads.push(opts.body);
    return {ok:true, json: async () => ({result:'Hasil uji', mode:opts.body.get('mode'),
      warnings:[], processing_ms:1, frame_width:640, frame_height:480})};
  },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8'), context);

async function waitFor(test) {
  const deadline = Date.now() + 2000;
  while (!test()) {
    if (Date.now() > deadline) throw new Error("Kondisi UI tidak tercapai");
    await new Promise(r => setTimeout(r, 5));
  }
}

(async () => {
  await waitFor(() => streams.length === 1 && streams[0].onmessage);
  assert.equal(streams.length, 1, 'satu koneksi stream ESP32 dibuka saat halaman dimuat');
  assert.ok(streams[0].url.includes('/api/esp32/stream'), 'memakai endpoint SSE, bukan polling');

  const source = streams[0];
  source.emit({ type: 'ready', mapping: [] });
  assert.ok(element('esp32Status').textContent.includes('siap'), 'status siap ditampilkan');

  // Backend memulai deteksi dari tombol fisik: halaman hanya menampilkan, tidak mengirim request.
  source.emit({ type: 'quick_started', mode: 'objek', label: 'Deteksi Objek' });
  await new Promise(r => setTimeout(r, 30));
  assert.equal(uploads.length, 0, 'event tombol fisik TIDAK boleh memicu runQuick (deteksi ganda)');
  assert.ok(element('esp32Status').textContent.includes('Deteksi Objek'), 'status tombol ditampilkan');

  source.emit({ type: 'quick_rejected', mode: 'rambu', label: 'Deteksi Rambu Lalu Lintas', reason: 'perintah sebelumnya masih berjalan' });
  assert.ok(element('esp32Status').textContent.includes('tidak dijalankan'));
  assert.equal(uploads.length, 0);

  // Hasil dari backend ditampilkan sebagai teks.
  source.emit({ type: 'quick_result', mode: 'objek', http_status: 200, result: 'Ada kursi di depan.', processing_ms: 850 });
  assert.equal(element('resultText').textContent, 'Ada kursi di depan.');
  assert.ok(element('quickStatus').textContent.includes('speaker ESP32'));

  // Pesan tak dikenal diabaikan diam-diam.
  source.emit({ type: 'quick', mode: 'objek' });
  source.emit({ type: 'tidak-ada' });
  await new Promise(r => setTimeout(r, 30));
  assert.equal(uploads.length, 0);

  // Jalur ESP32 putus: tombol layar HARUS tetap berfungsi.
  source.onerror();
  assert.ok(source.closed);
  assert.ok(element('esp32Status').textContent.includes('terputus'));
  assert.ok(buttons.every(b => !b.disabled), 'tombol layar tetap aktif setelah ESP32 putus');
  buttons[0].listeners.click();
  await waitFor(() => uploads.length === 1 && buttons.every(b => !b.disabled));
  assert.equal(uploads.length, 1, 'klik mouse tetap bekerja tanpa ESP32');
  assert.equal(uploads[0].get('mode'), 'objek');

  windowListeners.pagehide();
  console.log('PASS esp32: SSE tersambung, event tombol fisik hanya ditampilkan (tanpa deteksi ganda), ' +
              'hasil backend tampil, tombol layar bertahan saat ESP32 putus.');
})().catch(e => {console.error(e); process.exitCode=1;});
