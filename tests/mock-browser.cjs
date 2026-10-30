// Fake Puppeteer/Xvfb/FFmpeg to exercise the actual upstream-derived script.
const Module = require('module');
const EventEmitter = require('events');
const fs = require('fs');
const original = Module._load;
let captureClosed = false;
let xvfbOptions, chromeOptions, clearMetrics = false;
const mode = process.env.MOCK_MODE;
global.document = { getElementById: () => ({duration: global.mediaDuration}) };
global.mediaDuration = NaN;
const page = {
  _client: {send: async method => {if (method === 'Emulation.clearDeviceMetricsOverride') clearMetrics = true;}}, on: () => {}, goto: async () => {},
  setBypassCSP: async () => {}, waitForTimeout: async () => {},
  $eval: async selector => { if (selector === '.error-code') throw Error('no error'); },
  waitForSelector: async () => {}, click: async () => {}, close: async () => {},
  waitForFunction: async fn => {
    if (mode === 'timeout') throw Error('timeout');
    if (fn()) throw Error('NaN was incorrectly accepted');
    global.mediaDuration = 11065;
    if (!fn()) throw Error('valid duration was not accepted');
  },
  evaluate: async fn => fn()
};
class FakeXvfb { constructor(options) {xvfbOptions=options;} startSync() {} stopSync() {
  if (mode !== 'timeout' && !captureClosed) throw Error('Xvfb stopped before FFmpeg');
}}
const fakeChild = {spawn: (program,args) => {
  fs.writeFileSync(process.env.ARGS_FILE, JSON.stringify({program,args,xvfbOptions,chromeOptions,clearMetrics}));
  const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  setTimeout(() => {captureClosed=true; child.emit('close', mode === 'ffmpeg-fail' ? 1 : 0);}, 5);
  return child;
}};
Module._load = function(name, parent, main) {
  if (name === 'puppeteer') return {launch: async options => {chromeOptions=options; return {pages: async () => [page], close: async () => {}};}};
  if (name === 'xvfb') return FakeXvfb;
  if (name === 'child_process') return fakeChild;
  return original.apply(this, arguments);
};
