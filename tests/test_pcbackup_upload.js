const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../xiaomi-nas-plugin-pcbackup/src/ui/app.js'), 'utf8');
const boot = source.lastIndexOf('  bind();');
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
async function until(predicate) {
  for (let i = 0; i < 100; i++) {
    await flush();
    if (predicate()) return;
  }
  assert.fail('asynchronous upload runner did not reach the expected state');
}

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
    if (options.fetch) {
      const custom = options.fetch(url, opt, response);
      if (custom !== undefined) return custom;
    }
    if (url.includes('get_pool_info')) return Promise.resolve(response({ data: { webDAV: { poolRoot: '/pool0/data' } } }));
    const body = opt.body && JSON.parse(opt.body);
    let data = {};
    if (url.endsWith('/mkdirs')) data = { processed: body.paths.length, created: body.paths.length };
    if (url.endsWith('/native-upload') && body.action === 'prepare') {
      const token = (tokenId++).toString(16).padStart(32, '0');
      data = { token, temp_name: `pcb_${token}.temptd_1`, ready_name: `pcb_${token}.ready`, uploaded: false };
    }
    if (url.endsWith('/jobs')) data = [];
    if (url.endsWith('/native-upload') && body.action === 'finalize') data = { committed: true };
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

test('two directory batches overlap and out-of-order acknowledgements advance progress monotonically', async () => {
  const pending = [], updates = [];
  let active = 0, maxActive = 0;
  const h = harness({ fetch: (url, opt, response) => new Promise(resolve => {
    const size = JSON.parse(opt.body).paths.length;
    active++; maxActive = Math.max(maxActive, active);
    pending.push(() => { active--; resolve(response({ code: 0, data: { processed: size, created: 1 } })); });
  }) });
  const paths = Array.from({ length: 4001 }, (_, i) => [`folder-${i}/a.txt`, 5]);
  const p = h.prepareNativeDirectories(job, paths, (n, total, info) => updates.push({ n, total, ...info }));
  await flush();
  assert.equal(pending.length, 2);
  pending[1](); await flush();
  assert.equal(pending.length, 3);
  pending[2](); await flush();
  assert.deepEqual(updates.map(u => u.n), [2000, 2001]);
  pending[0]();
  const result = await p;
  assert.equal(maxActive, 2);
  assert.deepEqual(updates.map(u => u.n), [2000, 2001, 4001]);
  assert.deepEqual(updates.map(u => u.batch), [2, 3, 1]);
  assert.deepEqual(updates.map(u => u.completed), [1, 2, 3]);
  assert.ok(updates.every(u => u.total === 4001 && u.batches === 3));
  assert.equal(result.created, 3);
  assert.equal(h.timers.size, 0);
});

test('directory failure stops new batches and drains the other request before settling', async () => {
  const pending = [];
  const h = harness({ fetch: (url, opt, response) => new Promise(resolve => {
    const size = JSON.parse(opt.body).paths.length;
    pending.push(processed => resolve(response({ code: 0, data: { processed, created: 1 } })));
  }) });
  const paths = Array.from({ length: 6001 }, (_, i) => [`folder-${i}/a.txt`, 5]);
  let settled = false;
  const result = h.prepareNativeDirectories(job, paths).then(() => { settled = true; }, e => { settled = true; return e; });
  await flush();
  assert.equal(pending.length, 2);
  pending[0](0); await flush();
  assert.equal(settled, false);
  assert.equal(pending.length, 2);
  pending[1](2000);
  assert.match((await result).message, /未覆盖全部文件/);
  assert.equal(pending.length, 2);
  assert.equal(h.timers.size, 0);
});

test('black log shows preparation batches and upload phase only after all batches succeed', async () => {
  const pending = [];
  const h = harness({ fetch: (url, opt, response) => {
    if (url.endsWith('/mkdirs')) return new Promise(resolve => {
      const size = JSON.parse(opt.body).paths.length;
      pending.push(() => resolve(response({ code: 0, data: { processed: size, created: 0 } })));
    });
  } });
  const upload = Array.from({ length: 2001 }, (_, i) => [`new-${i}/a.txt`, 5]);
  Object.assign(h.state, { job: { ...job, name: 'documents' }, plan: { upload, upload_bytes: 10005, deleted: [] },
    sourcePath: 'D:\\docs', fileMap: Object.fromEntries(upload.map(([p]) => [p, entry])) });
  const logs = () => h.nodes.get('runLog').childNodes.map(n => n.textContent).join('\n');
  h.runUploadActive(); await flush();
  assert.equal(pending.length, 2);
  assert.match(logs(), /documents/);
  assert.match(logs(), /\[阶段\] 准备目录/);
  assert.match(logs(), /2 批并发/);
  pending[1](); await flush();
  assert.equal(h.native.length, 0);
  assert.match(logs(), /已完成 1 \/ 2 批.*累计处理 1 \/ 2001 个文件路径/);
  assert.doesNotMatch(logs(), /\[阶段\] 上传中/);
  pending[0](); await flush(); await flush();
  assert.match(logs(), /已完成 2 \/ 2 批.*累计处理 2001 \/ 2001 个文件路径/);
  assert.match(logs(), /\[阶段\] 上传中/);
  h.state.cancel = true; h.abortAllUploads();
  await until(() => !h.state.running); await flush();
  assert.match(logs(), /\[阶段\] 已取消/);
  assert.equal(h.timers.size, 0);
});

test('upload runner fills six slots and refills only after a file finishes', async () => {
  const h = harness();
  const upload = Array.from({ length: 8 }, (_, i) => [`file-${i}.txt`, 5]);
  Object.assign(h.state, { job, plan: { upload, upload_bytes: 40, deleted: [], skipped: 0 }, sourcePath: 'D:\\docs',
    fileMap: Object.fromEntries(upload.map(([p]) => [p, { ...entry, abs: 'D:\\docs\\' + p }])) });
  h.runUploadActive();
  for (let i = 0; i < 10 && h.native.length === 0; i++) await flush();
  assert.equal(h.native.length, 6);
  h.native[0].done({ size: 5, mtimeMs: 1000 }); await until(() => h.native.length === 7);
  assert.equal(h.native.length, 7);
  h.native[1].done({ size: 5, mtimeMs: 1000 }); await until(() => h.native.length === 8);
  assert.equal(h.native.length, 8);
  for (const task of h.native.slice(2)) task.done({ size: 5, mtimeMs: 1000 });
  await until(() => !h.state.running); await flush();
  assert.equal(h.state.running, false);
  const finish = h.requests.find(r => r.url.endsWith('/finish'));
  assert.equal(JSON.parse(finish.opt.body).uploaded, 8);
  assert.equal(h.timers.size, 0);
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
  assert.ok(h.requests.some(r => r.opt.body && JSON.parse(r.opt.body).action === 'finalize'));
  assert.equal(Object.keys(h.transfers).length, 0);
  assert.equal(h.timers.size, 0);
});

test('recovering a complete NAS stage avoids sending the PC file again', async () => {
  const h = harness({ fetch: async (url, opt, response) => response({ code: 0,
    data: opt.body && JSON.parse(opt.body).action === 'prepare' ? { token: 'b'.repeat(32), uploaded: true } : { committed: true } }) });
  await h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 0, () => {});
  assert.equal(h.native.length, 0);
  assert.equal(h.requests.filter(r => r.url.includes('get_pool_info')).length, 0);
});

test('lost finalize acknowledgement retries the same token without uploading again', async () => {
  const confirms = [];
  const h = harness({ fetch: (url, opt, response) => {
    if (opt.body && JSON.parse(opt.body).action === 'finalize') {
      confirms.push(JSON.parse(opt.body));
      if (confirms.length === 1) return Promise.reject(new Error('connection lost after commit'));
      return Promise.resolve(response({ code: 0, data: { committed: true } }));
    }
  } });
  const p = h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 0, () => {});
  await until(() => h.native.length === 1);
  h.native[0].done({}); await flush();
  assert.equal(confirms.length, 1);
  h.clock.tick(2000); await p;
  assert.equal(confirms.length, 2);
  assert.equal(confirms[0].token, confirms[1].token);
  assert.equal(h.native.length, 1);
  assert.equal(h.timers.size, 0);
});

test('cancelling during finalize retry delay settles immediately and ignores late callbacks', async () => {
  let confirms = 0;
  const h = harness({ fetch: (url, opt) => {
    if (opt.body && JSON.parse(opt.body).action === 'finalize') {
      confirms++; return Promise.reject(new Error('temporary failure'));
    }
  } });
  const p = h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 0, () => {});
  const rejected = assert.rejects(p, e => e.cancelled === true);
  await until(() => h.native.length === 1);
  h.native[0].done({}); await flush();
  assert.equal(confirms, 1);
  h.state.cancel = true; h.abortAllUploads(); await rejected;
  h.native[0].done({}); h.clock.tick(10000); await flush();
  assert.equal(confirms, 1);
  assert.equal(h.timers.size, 0);
});

test('cancel while finalize is in flight preserves run identity and ignores its late response', async () => {
  let acknowledge, signal;
  const h = harness({ fetch: (url, opt, response) => {
    if (opt.body && JSON.parse(opt.body).action === 'finalize') {
      signal = opt.signal;
      return new Promise(resolve => { acknowledge = () => resolve(response({ code: 0, data: { committed: true } })); });
    }
  } });
  Object.assign(h.state, { job, plan: { upload: [['a.txt', 5]], upload_bytes: 5, deleted: [] },
    resumeRunId: 'saved-run', sourcePath: 'D:\\docs', fileMap: { 'a.txt': entry } });
  h.runUploadActive(); await until(() => h.native.length === 1);
  h.native[0].done({}); await until(() => acknowledge);
  h.state.cancel = true; h.abortAllUploads(); await until(() => !h.state.running);
  assert.equal(signal.aborted, true);
  const finish = JSON.parse(h.requests.find(r => r.url.endsWith('/finish')).opt.body);
  assert.equal(finish.cancelled, true);
  assert.equal(finish.run_id, 'saved-run');
  assert.equal(finish.uploaded, 0);
  acknowledge(); await flush();
  assert.equal(h.requests.filter(r => r.url.endsWith('/commit')).length, 0);
  assert.equal(h.timers.size, 0);
});

test('source metadata change blocks finalize without retrying a different PC file', async () => {
  const h = harness();
  let stats = 0;
  h.bridge.getPcFileStats = (info, done) => done({ size: 5, mtimeMs: ++stats === 1 ? 1000 : 2000 });
  const p = h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 0, () => {});
  const rejected = assert.rejects(p, /PC 文件已变化/);
  await until(() => h.native.length === 1); h.native[0].done({}); await rejected;
  assert.equal(h.requests.filter(r => r.opt.body && JSON.parse(r.opt.body).action === 'finalize').length, 0);
  assert.equal(h.timers.size, 0);
});

test('a stale finalize token is surfaced once without retry or chunk fallback', async () => {
  let confirms = 0;
  const h = harness({ fetch: (url, opt) => {
    if (opt.body && JSON.parse(opt.body).action === 'finalize') {
      confirms++;
      return Promise.resolve({ ok: false, status: 409, json: async () => ({ message: 'stale token' }) });
    }
  } });
  const p = h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 0, () => {});
  const rejected = assert.rejects(p, e => e.status === 409);
  await until(() => h.native.length === 1); h.native[0].done({}); await rejected;
  assert.equal(confirms, 1);
  assert.equal(h.xhrs.length, 0);
  assert.equal(h.timers.size, 0);
});

test('exhausted finalize retries keep the same token and never re-upload', async () => {
  const tokens = [];
  const h = harness({ fetch: (url, opt) => {
    if (opt.body && JSON.parse(opt.body).action === 'finalize') {
      tokens.push(JSON.parse(opt.body).token);
      return Promise.reject(new Error('NAS unavailable'));
    }
  } });
  const p = h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 0, () => {});
  const rejected = assert.rejects(p, /NAS unavailable/);
  await until(() => h.native.length === 1); h.native[0].done({}); await flush();
  h.clock.tick(2000); await flush();
  h.clock.tick(4000); await rejected;
  assert.equal(tokens.length, 3);
  assert.equal(new Set(tokens).size, 1);
  assert.equal(h.native.length, 1);
  assert.equal(h.timers.size, 0);
});

test('empty native files retain the chunk commit path', async () => {
  const h = harness();
  h.bridge.getPcFileStats = (info, done) => done({ size: 0, mtimeMs: 1000 });
  Object.assign(h.state, { job, plan: { upload: [['empty.txt', 0]], upload_bytes: 0, deleted: [] },
    sourcePath: 'D:\\docs', fileMap: { 'empty.txt': { ...entry, size: 0 } } });
  h.runUploadActive(); await until(() => h.xhrs.length === 1);
  h.xhrs[0].status = 200; h.xhrs[0].onload(); await until(() => !h.state.running); await flush();
  assert.equal(h.native.length, 0);
  assert.equal(h.requests.filter(r => r.url.endsWith('/commit')).length, 1);
  assert.equal(h.requests.filter(r => r.opt.body && JSON.parse(r.opt.body).action === 'finalize').length, 0);
  assert.equal(h.timers.size, 0);
});

test('six slots stay occupied until finalize acknowledgement', async () => {
  const pending = [];
  const h = harness({ fetch: (url, opt, response) => {
    if (opt.body && JSON.parse(opt.body).action === 'finalize') {
      return new Promise(resolve => pending.push(() => resolve(response({ code: 0, data: { committed: true } }))));
    }
  } });
  const upload = Array.from({ length: 8 }, (_, i) => [`file-${i}.txt`, 5]);
  Object.assign(h.state, { job, plan: { upload, upload_bytes: 40, deleted: [] }, sourcePath: 'D:\\docs',
    fileMap: Object.fromEntries(upload.map(([p]) => [p, entry])) });
  h.runUploadActive(); await until(() => h.native.length === 6);
  h.native.slice(0, 6).forEach(n => n.done({})); await until(() => pending.length === 6);
  assert.equal(h.native.length, 6);
  pending[0](); await until(() => h.native.length === 7);
  assert.equal(h.native.length, 7);
  h.state.cancel = true; h.abortAllUploads(); await until(() => !h.state.running);
  await flush();
  assert.equal(h.timers.size, 0);
});

test('native idle timeout stops the task and does not promote an uncertain PUT', async () => {
  const h = harness();
  const p = h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 0, () => {});
  const rejected = assert.rejects(p, /超时/);
  await flush(); h.clock.tick(121000); await rejected;
  assert.equal(h.pauses.length, 1);
  assert.equal(h.pauses[0].callback, 'function');
  assert.equal(Object.keys(h.transfers).length, 0);
  assert.ok(!h.requests.some(r => r.opt.body && JSON.parse(r.opt.body).action === 'finalize'));
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
  assert.ok(maxActive <= 6);
  const requests = h.requests.filter(r => r.opt.body).map(r => ({ url: r.url, ...JSON.parse(r.opt.body) }));
  assert.deepEqual(requests.filter(r => r.url.endsWith('/mkdirs')).map(r => r.paths.length), [2000, 1]);
  assert.equal(requests.filter(r => r.action === 'finalize').length, 2001);
  assert.equal(requests.filter(r => r.action === 'complete').length, 0);
  assert.equal(requests.filter(r => r.url.endsWith('/commit')).length, 0);
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
  const upload = Array.from({ length: 5001 }, (_, i) => [`new-${i}/a.txt`, 5]);
  Object.assign(h.state, { job, plan: { upload, upload_bytes: 25005, deleted: [] },
    resumeRunId: 'saved-run', sourcePath: 'D:\\docs', fileMap: { 'a.txt': entry } });
  h.runUploadActive(); await flush();
  assert.equal(h.requests.filter(r => r.url.endsWith('/mkdirs')).length, 2);
  h.state.cancel = true; h.abortAllUploads(); await flush();
  assert.equal(h.state.running, false);
  const finish = h.requests.find(r => r.url.endsWith('/finish'));
  assert.ok(finish, 'cancelled preparation must settle the saved run too');
  assert.equal(JSON.parse(finish.opt.body).cancelled, true);
  assert.equal(JSON.parse(finish.opt.body).run_id, 'saved-run');
  assert.equal(h.native.length, 0);
  assert.equal(h.requests.filter(r => r.url.endsWith('/mkdirs')).length, 2);
  assert.ok(h.requests.filter(r => r.url.endsWith('/mkdirs')).every(r => r.opt.signal.aborted));
  assert.equal(h.timers.size, 0);
});
