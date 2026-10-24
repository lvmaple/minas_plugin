const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../xiaomi-nas-plugin-pcbackup/src/ui/app.js'), 'utf8');
const boot = source.lastIndexOf('  bind();');
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };

function harness(options = {}) {
  let now = 0, next = 1, tokenId = 1;
  const timers = new Map(), requests = [], xhrs = [], native = [], pauses = [];
  const clock = {
    set(fn, ms, interval) { const id = next++; timers.set(id, { fn, at: now + ms, interval }); return id; },
    clear(id) { timers.delete(id); },
    tick(ms) {
      const end = now + ms;
      while (true) {
        const due = [...timers].filter(([, v]) => v.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        now = due[1].at; timers.delete(due[0]);
        if (due[1].interval) timers.set(due[0], { ...due[1], at: now + due[1].interval });
        due[1].fn();
      }
      now = end;
    }
  };
  function node() {
    const value = { style: {}, childNodes: [], hidden: false, checked: false,
      scrollIntoView() {}, appendChild(n) { this.childNodes.push(n); },
      removeChild(n) { this.childNodes.splice(this.childNodes.indexOf(n), 1); },
      querySelectorAll() { return []; }, addEventListener() {}, remove() {} };
    Object.defineProperty(value, 'firstChild', { get() { return this.childNodes[0]; } });
    return value;
  }
  const nodes = new Map();
  const document = { getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    querySelector() { return { value: '' }; }, querySelectorAll() { return []; }, createElement: node };
  const bridge = {
    getPcFileList() {},
    getPcFileStats(info, done) { done({ size: 5, mtimeMs: 1000 }); },
    upLoadToNas(task, progress, done, fail) { native.push({ task, progress, done, fail }); },
    pauseUpload(tag, stop, done) { pauses.push({ tag, stop, callback: typeof done }); if (done) done(); }
  };
  const response = data => ({ ok: true, status: 200, json: async () => data });
  function fetch(url, opt = {}) {
    requests.push({ url, opt });
    if (options.fetch) return options.fetch(url, opt, response);
    if (url.includes('get_pool_info')) return Promise.resolve(response({ data: { webDAV: { poolRoot: '/pool0/data' } } }));
    const body = opt.body && JSON.parse(opt.body);
    let data = {};
    if (url.endsWith('/mkdirs')) data = { processed: body.paths.length, created: body.paths.length };
    if (url.endsWith('/native-upload') && body.action === 'prepare') {
      const token = (tokenId++).toString(16).padStart(32, '0');
      data = { token, temp_name: `pcb_${token}.temptd_1`, ready_name: `pcb_${token}.ready`, uploaded: false };
    }
    if (url.endsWith('/jobs')) data = [];
    return Promise.resolve(response({ code: 0, data }));
  }
  class Xhr {
    constructor() { this.upload = {}; xhrs.push(this); }
    open() {}
    send() {}
    abort() { if (this.onabort) this.onabort(); }
  }
  const window = { location: { pathname: '/plugin/123/pcbackup/index.html' }, frames: [], electronAPI: bridge };
  const context = vm.createContext({ window, document, fetch, AbortController, XMLHttpRequest: Xhr,
    Blob, ArrayBuffer, Uint8Array, Promise, console, Date: class extends Date { static now() { return now; } },
    setTimeout: (f, ms) => clock.set(f, ms), clearTimeout: id => clock.clear(id),
    setInterval: (f, ms) => clock.set(f, ms, ms), clearInterval: id => clock.clear(id) });
  vm.runInContext(source.slice(0, boot) + `
    window.testHooks = { state, api, xhrPut, abortAllUploads, uploadViaNative, runUploadActive,
      prepareNativeDirectories: typeof prepareNativeDirectories === 'function' ? prepareNativeDirectories : null,
      waitNativeUpload: typeof waitNativeUpload === 'function' ? waitNativeUpload : null,
      transfers: typeof _nativeTransfers === 'undefined' ? null : _nativeTransfers };
  })();`, context);
  const hooks = window.testHooks;
  hooks.state.config = { data_root: '/nas/pool0/u123/data' };
  return { ...hooks, clock, timers, requests, native, pauses, xhrs, nodes, bridge };
}

const job = { id: '1234abcd', destination: '/nas/pool0/u123/data/backup' };
const entry = { abs: 'D:\\docs\\a.txt', size: 5, mtime: 1000 };

test('directory preparation covers 4001 files in acknowledged batches', async () => {
  const h = harness();
  const paths = Array.from({ length: 4001 }, (_, i) => [`folder-${i}/a.txt`, 5]);
  await h.prepareNativeDirectories(job, paths);
  const batches = h.requests.map(r => JSON.parse(r.opt.body).paths);
  assert.ok(batches.length >= 3);
  assert.equal(batches.flat().length, 4001);
  assert.ok(batches.every(b => b.length <= 2000));
  assert.equal(batches.flat().at(-1), 'folder-4000/a.txt');
});

test('unacknowledged directory coverage blocks the actual upload runner', async () => {
  const h = harness({ fetch: async (url, opt, response) => {
    if (url.endsWith('/mkdirs')) return response({ code: 0, data: { created: 1, processed: 0 } });
    return response({ code: 0, data: {} });
  } });
  Object.assign(h.state, { job, plan: { upload: [['a.txt', 5]], upload_bytes: 5, deleted: [] },
    sourcePath: 'D:\\docs', fileMap: { 'a.txt': entry } });
  h.runUploadActive(); await flush();
  assert.equal(h.native.length, 0);
  assert.equal(h.state.running, false);
});

test('control request timeout aborts fetch and also covers a stalled JSON body', async () => {
  let signal;
  const h = harness({ fetch: async (url, opt) => {
    signal = opt.signal;
    return { ok: true, status: 200, json: () => new Promise(() => {}) };
  } });
  const p = h.api('GET', 'api/jobs');
  const rejected = assert.rejects(p, /超时/);
  await flush(); h.clock.tick(61000); await rejected;
  assert.equal(signal.aborted, true);
  assert.equal(h.timers.size, 0);
});

test('cancelling two XHRs aborts both and settles both promises', async () => {
  const h = harness();
  const a = h.xhrPut('data', new Blob(['a']));
  const b = h.xhrPut('data', new Blob(['b']));
  const results = Promise.allSettled([a, b]);
  h.abortAllUploads();
  const done = await results;
  assert.ok(done.every(r => r.status === 'rejected' && r.reason.cancelled));
  assert.ok(h.xhrs.every(x => x.timeout > 0));
});

test('native MOVE failure reconciles staged data without bridge PC reads', async () => {
  const h = harness();
  h.bridge.getPcFileStream = () => { throw new Error('unsafe fallback must stay disabled'); };
  const p = h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 3, () => {});
  await flush();
  assert.equal(h.native.length, 1);
  assert.equal(h.native[0].task.offset, 0, 'a new native staging path must start from zero');
  assert.ok(h.native[0].task.realdpath.startsWith('/pool0/data/.temp_upload_dir/'));
  h.native[0].fail({ code: -1 });
  await p;
  assert.ok(h.requests.some(r => r.opt.body && JSON.parse(r.opt.body).action === 'complete'));
  assert.equal(Object.keys(h.transfers).length, 0);
  assert.equal(h.timers.size, 0);
});

test('recovering a complete NAS stage avoids sending the PC file again', async () => {
  const h = harness({ fetch: async (url, opt, response) => response({ code: 0,
    data: opt.body && JSON.parse(opt.body).action === 'prepare' ? { token: 'b'.repeat(32), uploaded: true } : {} }) });
  await h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 0, () => {});
  assert.equal(h.native.length, 0);
  assert.equal(h.requests.filter(r => r.url.includes('get_pool_info')).length, 0);
});

test('native idle timeout stops the task and does not promote an uncertain PUT', async () => {
  const h = harness();
  const p = h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 0, () => {});
  const rejected = assert.rejects(p, /超时/);
  await flush(); h.clock.tick(121000); await rejected;
  assert.equal(h.pauses.length, 1);
  assert.equal(h.pauses[0].callback, 'function');
  assert.equal(Object.keys(h.transfers).length, 0);
  assert.ok(!h.requests.some(r => r.opt.body && JSON.parse(r.opt.body).action === 'complete'));
  h.native[0].done({ size: 5, mtimeMs: 1000 }); await flush();
  assert.equal(h.timers.size, 0, 'late callback must not restart or retain a task');
});

test('native cancellation settles the worker even without a terminal upload callback', async () => {
  const h = harness();
  const p = h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 0, () => {});
  const rejected = assert.rejects(p, e => e.cancelled === true);
  await flush(); h.state.cancel = true; h.abortAllUploads(); await rejected;
  assert.equal(h.pauses.length, 1);
  assert.equal(h.pauses[0].callback, 'function');
  assert.equal(Object.keys(h.transfers).length, 0);
  assert.equal(h.timers.size, 0);
});

test('native returned promise rejection settles immediately', async () => {
  const h = harness();
  h.bridge.upLoadToNas = () => Promise.reject(new Error('IPC unavailable'));
  await assert.rejects(h.waitNativeUpload(h.bridge, { tag: 'promise-rejection' }, () => {}), /IPC unavailable/);
  assert.equal(h.timers.size, 0);
});

test('repeating the same byte count does not disable the idle deadline', async () => {
  const h = harness();
  const p = h.waitNativeUpload(h.bridge, { tag: 'same-progress' }, () => {});
  const rejected = assert.rejects(p, /超时/);
  h.native[0].progress({ transferred: 1 });
  h.clock.tick(60000); h.native[0].progress({ transferred: 1 });
  h.clock.tick(61000); await rejected;
  assert.equal(h.pauses.length, 1);
});

test('an absolute native deadline remains active while bytes increase', async () => {
  const h = harness();
  const p = h.waitNativeUpload(h.bridge, { tag: 'total-deadline' }, () => {});
  const rejected = assert.rejects(p, /超时/);
  for (let i = 1; i <= 360; i++) {
    h.clock.tick(60000); h.native[0].progress({ transferred: i });
  }
  await rejected;
  assert.equal(Object.keys(h.transfers).length, 0);
  assert.equal(h.timers.size, 0);
});

test('cancel during credential fetch aborts it before any native task starts', async () => {
  let signal;
  const h = harness({ fetch: async (url, opt, response) => {
    if (url.includes('get_pool_info')) { signal = opt.signal; return new Promise(() => {}); }
    return response({ code: 0, data: { token: 'c'.repeat(32), uploaded: false } });
  } });
  const p = h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 0, () => {});
  const rejected = assert.rejects(p, e => e.cancelled === true);
  await flush(); h.state.cancel = true; h.abortAllUploads();
  await rejected;
  assert.equal(signal.aborted, true);
  assert.equal(h.native.length, 0);
  assert.equal(h.timers.size, 0);
});

test('cumulative XHR byte counts are converted to deltas once', async () => {
  const h = harness(), deltas = [];
  const p = h.xhrPut('data', new Blob(['hello']), n => deltas.push(n));
  h.xhrs[0].upload.onprogress({ loaded: 2 });
  h.xhrs[0].upload.onprogress({ loaded: 5 });
  h.xhrs[0].status = 200; h.xhrs[0].onload(); await p;
  assert.deepEqual(deltas, [2, 3]);
});

test('cancel also settles a source stat that never calls back', async () => {
  const h = harness();
  h.bridge.getPcFileStats = () => {};
  const p = h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 0, () => {});
  const rejected = assert.rejects(p, e => e.cancelled === true);
  await flush(); h.state.cancel = true; h.abortAllUploads(); await rejected;
  assert.equal(h.native.length, 0);
  assert.equal(h.timers.size, 0);
});

test('the actual upload runner completes 2001 native files after all directory batches', async () => {
  const h = harness();
  const upload = Array.from({ length: 2001 }, (_, i) => [`new-${i}/a.txt`, 5]);
  const fileMap = Object.fromEntries(upload.map(([rel]) => [rel, { ...entry, abs: 'D:\\docs\\' + rel.replaceAll('/', '\\') }]));
  let active = 0, maxActive = 0;
  h.bridge.upLoadToNas = (task, progress, done) => {
    active++; maxActive = Math.max(maxActive, active); h.native.push({ task });
    Promise.resolve().then(() => { active--; done({ size: 5, mtimeMs: 1000 }); });
  };
  Object.assign(h.state, { job, plan: { upload, upload_bytes: 10005, deleted: [], skipped: 0 }, sourcePath: 'D:\\docs', fileMap });
  h.runUploadActive();
  for (let i = 0; i < 10000 && h.state.running; i++) await flush();
  await flush();
  assert.equal(h.state.running, false);
  assert.equal(h.native.length, 2001);
  assert.ok(maxActive <= 3);
  const requests = h.requests.filter(r => r.opt.body).map(r => ({ url: r.url, ...JSON.parse(r.opt.body) }));
  assert.deepEqual(requests.filter(r => r.url.endsWith('/mkdirs')).map(r => r.paths.length), [2000, 1]);
  assert.equal(requests.filter(r => r.action === 'complete').length, 2001);
  assert.equal(requests.filter(r => r.url.endsWith('/commit')).length, 2001);
  const finish = requests.find(r => r.url.endsWith('/finish'));
  assert.equal(finish.uploaded, 2001);
  assert.equal(finish.failed, 0);
  assert.equal(Object.keys(h.transfers).length, 0);
  assert.equal(h.timers.size, 0);
});

test('cancelling directory preparation records cancellation and preserves the checkpoint', async () => {
  const h = harness({ fetch: async (url, opt, response) => {
    if (url.endsWith('/mkdirs')) return new Promise(() => {});
    return response({ code: 0, data: url.endsWith('/jobs') ? [] : { status: 'cancelled' } });
  } });
  Object.assign(h.state, { job, plan: { upload: [['a.txt', 5]], upload_bytes: 5, deleted: [] },
    resumeRunId: 'saved-run', sourcePath: 'D:\\docs', fileMap: { 'a.txt': entry } });
  h.runUploadActive(); await flush(); h.state.cancel = true; h.abortAllUploads(); await flush();
  assert.equal(h.state.running, false);
  const finish = h.requests.find(r => r.url.endsWith('/finish'));
  assert.ok(finish, 'cancelled preparation must settle the saved run too');
  assert.equal(JSON.parse(finish.opt.body).cancelled, true);
  assert.equal(JSON.parse(finish.opt.body).run_id, 'saved-run');
  assert.equal(h.native.length, 0);
  assert.equal(h.timers.size, 0);
});
