// Browser-independent interaction tests; not real camera/browser E2E tests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const elements = new Map();
let opens = 0, uploads = [], stopped = 0, failUpload = false;
function element(id) {
  if (elements.has(id)) return elements.get(id);
  const el = { id, value: '', hidden: false, disabled: false, textContent: '', dataset: {},
    videoWidth: 640, videoHeight: 480, readyState: 4, width: 640, height: 480,
    listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; },
    setAttribute() {}, pause() {}, play: async () => {},
    requestVideoFrameCallback(fn) { setTimeout(fn, 1); return 1; }, cancelVideoFrameCallback() {},
    replaceChildren() {}, add() {},
    getContext() { return { drawImage() {}, clearRect() {}, strokeRect() {}, fillRect() {}, fillText() {},
      measureText() { return {width: 30}; } }; },
    toBlob(fn) { fn(new Blob(['jpeg'], {type: 'image/jpeg'})); },
  };
  elements.set(id, el); return el;
}
const buttons = ['objek', 'rambu'].map(mode => { const b=element(mode); b.dataset.mode=mode; return b; });
const track = {readyState: 'live', stop() { stopped++; }, getSettings() { return { deviceId: 'camera-A' }; }};
const stream = { getVideoTracks: () => [track], getTracks: () => [track] };
const windowListeners = {};
const context = vm.createContext({
  console, setTimeout, clearTimeout, Blob, FormData, AbortController, crypto: require('node:crypto').webcrypto,
  Option: function(label, value) { this.label=label; this.value=value; },
  localStorage: {getItem: () => 'session-test', setItem() {}},
  document: {getElementById: element, querySelectorAll: () => buttons},
  navigator: {mediaDevices: {getUserMedia: async () => { opens++; return stream; }, enumerateDevices: async () => []}},
  window: {isSecureContext: true, speechSynthesis: {cancel() {}, speak() {}},
    addEventListener(type, fn) {windowListeners[type]=fn;}},
  SpeechSynthesisUtterance: function(text) {this.text=text;},
  WebSocket: class {static OPEN=1; close() {}}, location: {protocol:'http:', host:'localhost'},
  createImageBitmap: async () => ({width:640, height:480, close() {}}),
  fetch: async (url, opts) => {
    if (url === '/api/client_config') return {json: async () => ({camera_source:'browser', burst_gap_ms:1})};
    if (failUpload) throw new Error('test disconnect');
    uploads.push(opts.body);
    return {ok:true, json: async () => ({result:'Hasil uji', mode:opts.body.get('mode'),
      warnings:[], processing_ms:1, frame_width:640, frame_height:480})};
  },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8'), context);
(async () => {
  await new Promise(r => setTimeout(r, 20));
  assert.equal(opens, 1);
  const first = context.runQuick('objek');
  assert.ok(buttons.every(b => b.disabled));
  await context.runQuick('baca');
  await first;
  assert.equal(uploads.length, 1);
  assert.equal(uploads[0].getAll('image').length, 3);
  assert.equal(uploads[0].get('camera_id'), 'camera-A');
  await context.runQuick('objek'); await context.runQuick('rambu');
  assert.equal(uploads[1].getAll('image').length, 3);
  assert.equal(uploads[2].getAll('image').length, 1);
  assert.equal(opens, 1, 'switching modes must not reopen webcam');
  failUpload = true; await context.runQuick('baca');
  assert.equal(element('resultText').textContent, 'test disconnect');
  assert.ok(buttons.every(b => !b.disabled));
  failUpload = false; await context.runQuick('baca');
  assert.equal(element('resultText').textContent, 'Hasil uji');
  windowListeners.pagehide(); assert.equal(stopped, 1);
  console.log('PASS frontend: 2 modes, one camera, burst payloads, busy guard, error recovery, track cleanup.');
})().catch(e => {console.error(e); process.exitCode=1;});
