const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const generation = 'a'.repeat(32);

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
      events: {}, clicks: 0, querySelector() { return node(); }, querySelectorAll() { return []; },
      addEventListener(name, fn) { this.events[name] = fn; },
      click() { this.clicks++; }, remove() {} };
    Object.defineProperty(value, 'firstChild', { get() { return this.childNodes[0]; } });
    return value;
  }
  const nodes = new Map();
  const document = { getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    querySelector() { return { value: options.delMode || '' }; }, querySelectorAll() { return []; }, createElement: node };
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
    if (url.endsWith('/finish')) data = { phase: 'done', result: { status: body.cancelled ? 'cancelled' : 'ok' } };
    if (url.endsWith('/native-upload') && body.action === 'finalize') data = { committed: true };
    return Promise.resolve(response({ code: 0, data }));
  }
  class Xhr {
    constructor() { this.upload = {}; xhrs.push(this); }
    open(method, url) { this.method = method; this.url = url; }
    send(body) { this.body = body; }
    abort() { this.aborted = true; if (this.onabort) this.onabort(); }
  }
  const window = { location: { pathname: '/plugin/123/pcbackup/index.html' }, frames: [], electronAPI: options.noBridge ? null : bridge };
  const context = vm.createContext({ window, document, fetch, AbortController, XMLHttpRequest: Xhr,
    Blob, ArrayBuffer, Uint8Array, Promise, console, Date: class extends Date { static now() { return now; } },
    setTimeout: (f, ms) => clock.set(f, ms), clearTimeout: id => clock.clear(id),
    setInterval: (f, ms) => clock.set(f, ms, ms), clearInterval: id => clock.clear(id) });
  vm.runInContext(source.slice(0, boot) + `
    window.testHooks = { state, api, xhrPut, abortAllUploads, uploadViaNative, runUpload, runUploadActive, nativePick,
      readResume, renderJobs, doScan, openBackup, resumeBackup, onPicked, traverseEntry, initDropzone,
      prepareNativeDirectories: typeof prepareNativeDirectories === 'function' ? prepareNativeDirectories : null,
      waitNativeUpload: typeof waitNativeUpload === 'function' ? waitNativeUpload : null,
      transfers: typeof _nativeTransfers === 'undefined' ? null : _nativeTransfers };
  })();`, context);
  const hooks = window.testHooks;
  hooks.state.config = { data_root: '/nas/pool0/u123/data' };
  hooks.state.plan = { generation };
  return { ...hooks, clock, timers, requests, native, pauses, xhrs, nodes, bridge };
}

const job = { id: '1234abcd', destination: '/nas/pool0/u123/data/backup' };
const entry = { abs: 'D:\\docs\\a.txt', size: 5, mtime: 1000 };

function selectionHarness(options = {}) {
  return harness({ ...options, fetch: (url, opt, response) => {
    if (options.fetch) {
      const custom = options.fetch(url, opt, response);
      if (custom !== undefined) return custom;
    }
    if (url.includes('/scan-status')) return Promise.resolve(response({ code: 0, data: { phase: 'queued' } }));
  } });
}

test('a late directory selection cannot replace the latest source or plan', async () => {
  const h = selectionHarness(), callbacks = {};
  h.state.job = { ...job, exclude: [], pc_path: 'D:\\old' };
  h.bridge.getPcFileList = (input, progress, done) => { callbacks[input.pcPath] = done; };
  h.nativePick('D:\\docs'); h.nativePick('E:\\docs');
  callbacks['E:\\docs']({ files: [{ fullPath: 'E:\\docs\\new.txt', size: 5 }] }); await flush();
  callbacks['D:\\docs']({ files: [{ fullPath: 'D:\\docs\\old.txt', size: 5 }] }); await flush();
  assert.equal(h.state.sourcePath, 'E:\\docs');
  assert.equal(h.state.entries[0].rel, 'new.txt');
  assert.equal(h.requests.filter(r => r.url.endsWith('/scan')).length, 1);
  assert.equal(h.requests.filter(r => r.opt.method === 'PATCH').length, 1);
});

test('the latest selection wins even when an earlier source PATCH is already in flight', async () => {
  let serverPath = 'C:\\old', oldAck, latestPatches = 0;
  const h = selectionHarness({ fetch: (url, opt, response) => {
    if (opt.method !== 'PATCH') return;
    const body = JSON.parse(opt.body);
    if (body.pc_path === 'D:\\docs') {
      serverPath = body.pc_path;
      return new Promise(resolve => { oldAck = () => resolve(response({ code: 0, data: {} })); });
    }
    latestPatches++;
    if (body.expected_pc_path !== serverPath) return Promise.resolve({ ok: false, status: 409,
      json: async () => ({ code: 409, message: 'source changed', data: { reason: 'source_changed', pc_path: serverPath } }) });
    serverPath = body.pc_path;
    return Promise.resolve(response({ code: 0, data: {} }));
  } });
  h.state.job = { ...job, exclude: [], pc_path: serverPath };
  h.bridge.getPcFileList = (input, progress, done) => done({ files: [{ fullPath: input.pcPath + '\\a.txt', size: 5 }] });
  h.nativePick('D:\\docs'); await flush();
  h.nativePick('E:\\docs'); await flush(); await flush();
  oldAck(); await flush();
  assert.equal(serverPath, 'E:\\docs');
  assert.equal(h.state.sourcePath, 'E:\\docs');
  assert.equal(latestPatches, 2);
  assert.equal(h.requests.filter(r => r.url.endsWith('/scan')).length, 1);
});

test('a late browser filtering batch cannot overwrite a newer selection', async () => {
  const h = selectionHarness({ noBridge: true }); h.state.job = { ...job, exclude: [] };
  const old = Array.from({ length: 3001 }, (_, i) => ({ name: 'old' + i, webkitRelativePath: 'docs/old' + i, size: 1, lastModified: 1000 }));
  h.onPicked(old, 'docs');
  h.onPicked([{ name: 'new.txt', webkitRelativePath: 'docs/new.txt', size: 1, lastModified: 1000 }], 'docs');
  h.clock.tick(0); await flush();
  assert.equal(h.state.entries.length, 1);
  assert.equal(h.state.entries[0].rel, 'new.txt');
  assert.equal(h.requests.filter(r => r.url.endsWith('/scan')).length, 1);
});

test('repicking during upload preserves the active native source', () => {
  const h = harness(); h.state.job = job; h.state.running = true; h.state.sourcePath = 'D:\\docs';
  h.onPicked([{ name: 'other.txt', size: 5, lastModified: 1000 }], 'other');
  assert.equal(h.state.sourcePath, 'D:\\docs');
  assert.equal(h.requests.length, 0);
});

test('a drag read error stops the whole scan instead of proposing deletion', async () => {
  const h = selectionHarness({ noBridge: true }); h.state.job = { ...job, exclude: [] }; h.initDropzone();
  let batch = 0;
  const root = { isDirectory: true, name: 'docs', fullPath: '/docs', createReader() {
    return { readEntries(done) { done(batch++ === 0 ? [
      { isFile: true, fullPath: '/docs/kept.txt', file(done) { done({ name: 'kept.txt', size: 5, lastModified: 1000 }); } },
      { isFile: true, fullPath: '/docs/existing.txt', file(done, fail) { fail(new Error('permission denied')); } }
    ] : []); } };
  } };
  h.nodes.get('dropzone').events.drop({ preventDefault() {}, stopPropagation() {},
    dataTransfer: { items: [{ webkitGetAsEntry: () => root }] } });
  await flush();
  assert.equal(h.requests.filter(r => r.url.endsWith('/scan')).length, 0);
  assert.ok(h.nodes.get('toasts').childNodes.some(n => n.textContent.includes('existing.txt')));
});

test('a successfully read empty native source can still generate a deletion plan', async () => {
  const h = selectionHarness(); h.state.job = { ...job, exclude: [], pc_path: 'D:\\docs' };
  h.bridge.getPcFileList = (input, progress, done) => done({ files: [] });
  h.nativePick('D:\\docs'); await flush();
  const scan = h.requests.find(r => r.url.endsWith('/scan'));
  assert.ok(scan); assert.deepEqual(JSON.parse(scan.opt.body).files, []);
});

test('a successfully read empty dropped folder can still generate a deletion plan', async () => {
  const h = selectionHarness({ noBridge: true }); h.state.job = { ...job, exclude: [] }; h.initDropzone();
  const root = { isDirectory: true, name: 'docs', fullPath: '/docs', createReader: () => ({ readEntries: done => done([]) }) };
  h.nodes.get('dropzone').events.drop({ preventDefault() {}, stopPropagation() {},
    dataTransfer: { items: [{ webkitGetAsEntry: () => root }] } });
  await flush();
  const scan = h.requests.find(r => r.url.endsWith('/scan'));
  assert.ok(scan); assert.deepEqual(JSON.parse(scan.opt.body).files, []);
});

function browserFile(mtime = 1000) {
  return { name: 'a.txt', webkitRelativePath: 'docs/a.txt', size: 5, lastModified: mtime,
    slice: (start, end) => new Blob(['hello'.slice(start, end)]) };
}

function browserResumeInfo() {
  return { source_kind: 'browser', source_path: '', run_id: 'browser-run', generation,
    del_mode: 'del', files: [['a.txt', 5, 1000]], plan: { generation, folder: 'docs', upload: [['a.txt', 5]],
      deleted: [], upload_bytes: 5, previous_uploaded: 0, previous_bytes: 0, skipped: 0 } };
}

test('changed browser files require a new scan instead of mixing old chunks', async () => {
  const info = browserResumeInfo();
  const h = selectionHarness({ noBridge: true, fetch: (url, opt, response) => {
    if (url.includes('/resume?')) return Promise.resolve(response({ code: 0, data: { phase: 'done', result: info } }));
  } });
  h.openBackup({ ...job, name: 'browser', exclude: [], resume_available: true }); await flush();
  h.resumeBackup(); h.onPicked([browserFile(2000)], 'docs'); await flush();
  assert.equal(h.xhrs.length, 0);
  assert.equal(h.nodes.get('askModal').hidden, false);
  assert.equal(h.requests.filter(r => r.url.endsWith('/scan')).length, 0);
  h.nodes.get('askOk').onclick(); await flush();
  const scan = h.requests.find(r => r.url.endsWith('/scan'));
  assert.ok(scan); assert.equal(JSON.parse(scan.opt.body).replace_resume, true);
  assert.equal(h.state.resumeRunId, null);
});

test('browser resume with only final work left needs no new folder selection', async () => {
  const info = browserResumeInfo(); info.files = []; info.plan.upload = []; info.plan.upload_bytes = 0;
  const h = harness({ noBridge: true, fetch: (url, opt, response) => {
    if (url.includes('/resume?')) return Promise.resolve(response({ code: 0, data: { phase: 'done', result: info } }));
  } });
  h.openBackup({ ...job, name: 'browser', exclude: [], resume_available: true }); await flush();
  h.resumeBackup(); await until(() => !h.state.running);
  assert.equal(h.nodes.get('dirInput')?.clicks || 0, 0);
  assert.equal(h.xhrs.length, 0);
  assert.equal(h.nodes.get('runPhase').textContent, '完成');
});

test('a fresh browser run saves its plan before sending file bytes', async () => {
  const h = harness({ noBridge: true, fetch: (url, opt, response) => {
    if (url.endsWith('/resume')) return Promise.resolve(response({ code: 0, data: { run_id: 'browser-run' } }));
  } });
  Object.assign(h.state, { job, plan: { generation, scan_id: 'scan', upload: [['a.txt', 5]], deleted: [], upload_bytes: 5 },
    fileMap: { 'a.txt': { file: browserFile(), size: 5, mtime: 1000 } } });
  h.runUpload(); await flush();
  const saved = h.requests.find(r => r.url.endsWith('/resume'));
  assert.ok(saved);
  assert.equal(JSON.parse(saved.opt.body).source_kind, 'browser');
  assert.equal(h.state.resumeRunId, 'browser-run');
  assert.equal(h.xhrs.length, 1);
  h.state.cancel = true; h.abortAllUploads(); await flush();
});

test('browser reopen reselects the original directory and resumes without rescanning', async () => {
  const info = browserResumeInfo();
  const h = harness({ noBridge: true, fetch: (url, opt, response) => {
    if (url.includes('/resume?')) return Promise.resolve(response({ code: 0, data: { phase: 'done', request_id: 'b'.repeat(32), result: info } }));
    if (url.includes('/part?')) return Promise.resolve(response({ code: 0, data: { size: 2 } }));
  } });
  h.openBackup({ ...job, name: 'browser', exclude: [], resume_available: true }); await flush();
  assert.equal(h.nodes.get('btnResume').disabled, false);
  h.resumeBackup();
  assert.equal(h.nodes.get('dirInput').clicks, 1);
  h.onPicked([browserFile()], 'docs'); await until(() => h.xhrs.length === 1);
  assert.equal(h.requests.filter(r => r.url.endsWith('/scan') || r.url.endsWith('/resume')).length, 0);
  assert.equal(h.state.resumeRunId, info.run_id);
  assert.equal(h.xhrs.length, 1);
  assert.ok(h.xhrs[0].url.endsWith('offset=2'));
  assert.equal(await h.xhrs[0].body.text(), 'llo');
  h.xhrs[0].status = 200; h.xhrs[0].onload(); await until(() => !h.state.running);
  assert.equal(h.nodes.get('runPhase').textContent, '完成');
});

test('same-page browser continuation refreshes completed progress before reselecting', async () => {
  const fresh = browserResumeInfo(); fresh.plan.previous_uploaded = 1; fresh.plan.previous_bytes = 5;
  const h = harness({ noBridge: true, fetch: (url, opt, response) => {
    if (url.includes('/resume?')) return Promise.resolve(response({ code: 0, data: { phase: 'done', result: fresh } }));
    if (url.includes('/part?')) return Promise.resolve(response({ code: 0, data: { size: 2 } }));
  } });
  const stale = browserResumeInfo(); stale.needsRefresh = true; stale.files.push(['completed.txt', 5, 1000]);
  Object.assign(h.state, { job, resumeRunId: stale.run_id, resumeInfo: stale });
  h.onPicked([browserFile(), { name: 'completed.txt', webkitRelativePath: 'docs/completed.txt', size: 5, lastModified: 1000 }], 'docs');
  await until(() => h.xhrs.length === 1);
  assert.equal(h.requests.filter(r => r.url.endsWith('/scan')).length, 0);
  assert.equal(h.state.plan.previous_uploaded, 1);
  assert.ok(h.xhrs[0].url.includes('a.txt'));
  assert.ok(h.xhrs[0].url.endsWith('offset=2'));
  h.state.cancel = true; h.abortAllUploads(); await until(() => !h.state.running);
});

test('type conflicts are preserved before directory batches or native uploads', async () => {
  const h = harness({ delMode: 'del', fetch: (url, opt, response) => {
    if (url.endsWith('/prepare-paths')) return Promise.resolve(response({ code: 0, data: { archived: 1 } }));
    if (url.endsWith('/deleted')) return Promise.resolve(response({ code: 0, data: [{ p: 'foo/bar.txt', status: 'renamed' }] }));
  } });
  Object.assign(h.state, { job, sourcePath: 'D:\\docs', resumeRunId: 'saved',
    plan: { generation, upload: [['foo', 5]], deleted: ['foo/bar.txt'], type_archives: { foo: 'foo.12345678.del' }, upload_bytes: 5 },
    fileMap: { foo: { ...entry, abs: 'D:\\docs\\foo' } } });
  h.runUploadActive(); await until(() => h.native.length === 1);
  const at = suffix => h.requests.findIndex(r => r.url.endsWith(suffix));
  assert.ok(at('/prepare-paths') > at('/deleted'));
  assert.ok(at('/mkdirs') > at('/prepare-paths'));
  assert.ok(at('/native-upload') > at('/mkdirs'));
  assert.equal(h.native.length, 1);
  h.native[0].done({}); await until(() => !h.state.running);
  assert.equal(h.nodes.get('runPhase').textContent, '完成');
});

test('cancelling type preparation settles without starting native uploads', async () => {
  const h = harness({ fetch: (url) => {
    if (url.endsWith('/prepare-paths')) return new Promise(() => {});
  } });
  Object.assign(h.state, { job, sourcePath: 'D:\\docs', resumeRunId: 'saved',
    plan: { generation, upload: [['a.txt', 5]], deleted: [], type_archives: { foo: 'foo.12345678.del' }, upload_bytes: 5 },
    fileMap: { 'a.txt': entry } });
  h.runUploadActive(); await until(() => h.requests.some(r => r.url.endsWith('/prepare-paths')));
  h.state.cancel = true; h.abortAllUploads(); await until(() => !h.state.running);
  assert.equal(h.native.length, 0);
  assert.equal(h.nodes.get('runPhase').textContent, '已取消');
  assert.equal(h.timers.size, 0);
});

test('pending resume summaries hide provisional counts and rejoin the resume identity', async () => {
  const requestId = 'c'.repeat(32);
  const info = { run_id: 'saved', files: [], plan: { previous_uploaded: 50000 } };
  const h = harness({ fetch: (url, opt, response) => {
    if (url.includes('/resume?')) return Promise.resolve(response({ code: 0,
      data: { phase: 'done', request_id: requestId, result: info } }));
  } });
  const pending = { ...job, name: 'saved', files: 0, bytes: 0, summary_pending: true,
    resume_check_pending: requestId, finish_pending: '', resume_available: true };
  h.state.jobs = [pending]; h.renderJobs();
  const html = h.nodes.get('jobList').innerHTML;
  assert.ok(html.includes('续传核对中'));
  assert.ok(html.includes('查看续传进度'));
  assert.ok(!html.includes('0 个文件'));
  h.state.job = pending;
  assert.deepEqual(await h.readResume(pending), info);
  assert.equal(new URL(h.requests[0].url, 'http://local').searchParams.get('request_id'), requestId);
  assert.ok(h.requests.every(r => !r.url.includes('/finish')));
});

for (const approved of [false, true]) {
  test('damaged checkpoint rescan requires explicit confirmation: ' + approved, async () => {
    const submitted = [];
    const h = harness({ fetch: (url, opt, response) => {
      if (url.endsWith('/scan')) {
        submitted.push(JSON.parse(opt.body));
        if (submitted.length === 1) return Promise.resolve({ ok: false, status: 409,
          json: async () => ({ message: 'saved plan damaged', data: { reason: 'resume_corrupt' } }) });
        return Promise.resolve(response({ code: 0, data: { started: true } }));
      }
      if (url.endsWith('/scan-status')) return Promise.resolve(response({ code: 0, data: { phase: 'queued' } }));
    } });
    Object.assign(h.state, { job, folder: 'docs', entries: [{ rel: 'a.txt', size: 5, mtime: 1000 }] });
    h.doScan(false, false); await flush();
    assert.equal(submitted.length, 1);
    assert.equal(h.nodes.get('askModal').hidden, false);
    h.nodes.get(approved ? 'askOk' : 'askNo').onclick(); await flush();
    assert.equal(submitted.length, approved ? 2 : 1);
    if (approved) assert.equal(submitted[1].replace_resume, true);
    else assert.ok(!h.state.discardResumeOnRun);
    h.state.scanToken++; h.clock.tick(5000); await flush();
    assert.equal(h.timers.size, 0);
  });
}

test('remembering a new PC source finishes before creating its fenced scan plan', async () => {
  let remember;
  const h = harness({ fetch: (url, opt, response) => {
    if (opt.method === 'PATCH') return new Promise(resolve => { remember = () => resolve(response({ code: 0 })); });
    if (url.endsWith('/scan-status')) return Promise.resolve(response({ code: 0, data: { phase: 'queued' } }));
  } });
  h.state.job = { ...job, pc_path: 'D:\\old' };
  h.bridge.getPcFileList = (info, progress, done) => done({
    files: [{ fullPath: 'D:\\docs\\a.txt', size: 5 }]
  });
  h.nativePick('D:\\docs');
  await until(() => remember);
  assert.equal(h.requests.filter(r => r.url.endsWith('/scan')).length, 0);
  remember();
  await until(() => h.requests.some(r => r.url.endsWith('/scan')));
  assert.equal(h.state.job.pc_path, 'D:\\docs');
  assert.equal(h.state.sourcePath, 'D:\\docs');
});

test('late damaged-plan response cannot rescan a newer active upload', async () => {
  let oldReply;
  const scans = [];
  const h = harness({ fetch: (url, opt, response) => {
    if (url.endsWith('/scan')) {
      scans.push(JSON.parse(opt.body));
      const bad = () => ({ ok: false, status: 409,
        json: async () => ({ message: 'damaged old plan', data: { reason: 'resume_corrupt' } }) });
      if (scans.length === 1) return new Promise(resolve => { oldReply = () => resolve(bad()); });
      if (scans.length === 2) return Promise.resolve(bad());
      return Promise.resolve(response({ code: 0, data: { started: true } }));
    }
    if (url.endsWith('/scan-status')) return Promise.resolve(response({ code: 0, data: { phase: 'queued' } }));
  } });
  Object.assign(h.state, { job, folder: 'docs', entries: [{ rel: 'a.txt', size: 5, mtime: 1000 }] });
  h.doScan(false, false); await flush();
  h.state.entries = [{ rel: 'b.txt', size: 5, mtime: 1000 }];
  h.doScan(false, false); await flush();
  h.nodes.get('askOk').onclick(); await flush();
  assert.equal(scans.length, 3);
  Object.assign(h.state, { plan: { generation, upload: [['b.txt', 5]], upload_bytes: 5, deleted: [] },
    fileMap: { 'b.txt': { file: new Blob(['world']), size: 5, mtime: 1000 } } });
  h.runUploadActive(); await until(() => h.xhrs.length === 1);
  oldReply(); await flush();
  assert.equal(h.nodes.get('askModal').hidden, true, 'old response must not open another rescan dialog');
  assert.equal(scans.length, 3);
  assert.equal(h.state.running, true);
  assert.ok(!h.xhrs[0].aborted);
  h.state.cancel = true; h.abortAllUploads(); h.state.scanToken++; await flush();
  h.clock.tick(5000); await flush();
});

test('an already opened rescan confirmation cannot replace an active upload', async () => {
  let scans = 0;
  const h = harness({ fetch: url => {
    if (url.endsWith('/scan')) {
      scans++;
      return Promise.resolve({ ok: false, status: 409,
        json: async () => ({ message: 'damaged old plan', data: { reason: 'resume_corrupt' } }) });
    }
  } });
  Object.assign(h.state, { job, folder: 'docs', entries: [{ rel: 'a.txt', size: 5, mtime: 1000 }] });
  h.doScan(false, false); await flush();
  const confirm = h.nodes.get('askOk').onclick;
  Object.assign(h.state, { plan: { generation, upload: [['b.txt', 5]], upload_bytes: 5, deleted: [] },
    fileMap: { 'b.txt': { file: new Blob(['world']), size: 5, mtime: 1000 } } });
  h.runUploadActive(); await until(() => h.xhrs.length === 1);
  confirm(); await flush();
  h.doScan(false, false); await flush();
  assert.equal(scans, 1, 'neither old confirmation nor direct scan may interrupt an upload');
  assert.equal(h.state.running, true);
  h.state.cancel = true; h.abortAllUploads(); h.state.scanToken++; await flush();
  h.clock.tick(5000); await flush();
});

test('a late successful scan response cannot start polling a newer scan', async () => {
  let oldReply, scans = 0;
  const h = harness({ fetch: (url, opt, response) => {
    if (url.endsWith('/scan')) {
      if (++scans === 1) return new Promise(resolve => {
        oldReply = () => resolve(response({ code: 0, data: { started: true } }));
      });
      return Promise.resolve(response({ code: 0, data: { started: true } }));
    }
    if (url.endsWith('/scan-status')) return Promise.resolve(response({ code: 0, data: { phase: 'queued' } }));
  } });
  Object.assign(h.state, { job, folder: 'docs', entries: [{ rel: 'a.txt', size: 5, mtime: 1000 }] });
  h.doScan(false, false); await flush(); h.doScan(false, false); await flush();
  const before = h.requests.filter(r => r.url.endsWith('/scan-status')).length;
  oldReply(); await flush();
  assert.equal(h.requests.filter(r => r.url.endsWith('/scan-status')).length, before);
  h.state.scanToken++; h.clock.tick(5000); await flush();
  assert.equal(h.timers.size, 0);
});

test('chunk retries and commit keep the original plan generation', async () => {
  const h = harness();
  Object.assign(h.state, { job, plan: { generation, upload: [['a.txt', 5]], upload_bytes: 5, deleted: [] },
    fileMap: { 'a.txt': { file: new Blob(['hello']), size: 5, mtime: 1000 } } });
  h.runUploadActive();
  await until(() => h.xhrs.length === 1);
  const first = h.xhrs[0];
  assert.equal(new URL(first.url, 'http://local').searchParams.get('generation'), generation);
  first.onerror();
  await flush();
  h.state.plan.generation = 'b'.repeat(32);
  h.clock.tick(2000);
  await until(() => h.xhrs.length === 2);
  assert.equal(h.xhrs[1].url, first.url);
  assert.equal(await h.xhrs[1].body.text(), 'hello');
  h.xhrs[1].status = 200; h.xhrs[1].onload();
  await until(() => !h.state.running);
  for (const route of ['/commit', '/finish']) {
    const request = h.requests.find(r => r.url.endsWith(route));
    assert.equal(JSON.parse(request.opt.body).generation, generation);
  }
  assert.equal(h.timers.size, 0);
});

test('a stale chunk plan stops new files without retrying or committing', async () => {
  const h = harness();
  const upload = Array.from({ length: 12 }, (_, i) => [i + '.txt', 5]);
  Object.assign(h.state, { job, plan: { generation, upload, upload_bytes: 60, deleted: [] },
    fileMap: Object.fromEntries(upload.map(([rel]) => [rel,
      { file: new Blob(['hello']), size: 5, mtime: 1000 }])) });
  h.runUploadActive();
  await until(() => h.xhrs.length === 6);
  for (const xhr of h.xhrs) {
    xhr.status = 409;
    xhr.responseText = JSON.stringify({ message: 'stale plan', data: { reason: 'transfer_stale' } });
    xhr.onload();
  }
  await flush();
  assert.equal(h.xhrs.length, 6);
  assert.equal(h.state.running, false);
  assert.equal(h.requests.filter(r => r.url.endsWith('/commit')).length, 0);
  assert.equal(h.requests.filter(r => r.url.endsWith('/finish')).length, 0);
  h.clock.tick(5000); // dismiss the expected error toast, with no retry work
  await flush();
  assert.equal(h.timers.size, 0);
});

test('one stale response cancels the other five workers even without callbacks', async () => {
  const h = harness();
  const upload = Array.from({ length: 12 }, (_, i) => [i + '.txt', 5]);
  Object.assign(h.state, { job, plan: { generation, upload, upload_bytes: 60, deleted: [] },
    fileMap: Object.fromEntries(upload.map(([rel]) => [rel,
      { file: new Blob(['hello']), size: 5, mtime: 1000 }])) });
  h.runUploadActive();
  await until(() => h.xhrs.length === 6);
  h.xhrs[0].status = 409;
  h.xhrs[0].responseText = JSON.stringify({ data: { reason: 'transfer_stale' } });
  h.xhrs[0].onload();
  await flush();
  assert.equal(h.state.running, false);
  assert.equal(h.xhrs.length, 6);
  assert.equal(h.xhrs.filter(x => x.aborted).length, 5);
  assert.equal(h.requests.filter(r => r.url.endsWith('/commit') || r.url.endsWith('/finish')).length, 0);
});

test('cancelling chunk uploads keeps the generation and sends no commit', async () => {
  const h = harness();
  Object.assign(h.state, { job, plan: { generation, upload: [['a.txt', 5]], upload_bytes: 5, deleted: [] },
    fileMap: { 'a.txt': { file: new Blob(['hello']), size: 5, mtime: 1000 } } });
  h.runUploadActive();
  await until(() => h.xhrs.length === 1);
  h.state.cancel = true; h.abortAllUploads();
  await until(() => !h.state.running);
  await flush();
  const finish = JSON.parse(h.requests.find(r => r.url.endsWith('/finish')).opt.body);
  assert.equal(finish.cancelled, true);
  assert.equal(finish.generation, generation);
  assert.equal(h.requests.filter(r => r.url.endsWith('/commit')).length, 0);
  assert.equal(h.timers.size, 0);
});

test('native prepare carries the plan generation before starting the host upload', async () => {
  const h = harness();
  const uploading = h.uploadViaNative(job, entry, 'a.txt', 5, 1000, 0, () => {});
  await until(() => h.native.length === 1);
  const prepare = h.requests.find(r => r.url.endsWith('/native-upload'));
  assert.equal(JSON.parse(prepare.opt.body).generation, generation);
  h.native[0].done({});
  await uploading;
});

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
  Object.assign(h.state, { job: { ...job, name: 'documents' }, plan: { generation, upload, upload_bytes: 10005, deleted: [] },
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
  Object.assign(h.state, { job, plan: { generation, upload, upload_bytes: 40, deleted: [], skipped: 0 }, sourcePath: 'D:\\docs',
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
  Object.assign(h.state, { job, plan: { generation, upload: [['a.txt', 5]], upload_bytes: 5, deleted: [] },
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
  Object.assign(h.state, { job, plan: { generation, upload: [['a.txt', 5]], upload_bytes: 5, deleted: [] },
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
  Object.assign(h.state, { job, plan: { generation, upload: [['empty.txt', 0]], upload_bytes: 0, deleted: [] },
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
  Object.assign(h.state, { job, plan: { generation, upload, upload_bytes: 40, deleted: [] }, sourcePath: 'D:\\docs',
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
  Object.assign(h.state, { job, plan: { generation, upload, upload_bytes: 10005, deleted: [], skipped: 0 }, sourcePath: 'D:\\docs', fileMap });
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
  Object.assign(h.state, { job, plan: { generation, upload, upload_bytes: 25005, deleted: [] },
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
  await flush();
  assert.equal(h.timers.size, 0);
});

function completedPlan(h) {
  Object.assign(h.state, { job, resumeRunId: 'saved-run', plan: { generation, upload: [], deleted: [],
    previous_uploaded: 50000, previous_bytes: 50000, upload_bytes: 0, skipped: 0 } });
}

test('50000-file finish survives intermittent network failures between healthy replies', async () => {
  let calls = 0;
  const h = harness({ fetch: (url, opt, response) => {
    if (url.endsWith('/finish') || url.includes('/finish-status?')) {
      calls++;
      if ([2, 4, 6].includes(calls)) return Promise.reject(new Error('temporary network failure'));
      return Promise.resolve(response({ code: 0, data: { phase: calls < 7 ? 'running' : 'done',
        result: calls >= 7 ? { status: 'ok' } : undefined } }));
    }
  } });
  completedPlan(h); h.runUploadActive(); await flush();
  for (let i = 0; i < 12; i++) { h.clock.tick(1000); await flush(); }
  assert.equal(h.nodes.get('runPhase').textContent, '完成');
  assert.equal(calls, 7);
  const bodies = h.requests.filter(r => r.url.endsWith('/finish')).map(r => r.opt.body);
  assert.equal(new Set(bodies).size, 1, 'network retries retain the original identity and stats');
  assert.equal(h.timers.size, 0);
});

for (const malformed of [false, true]) {
  test('persistent finish polling failures stop despite successful admission ACKs: ' + malformed, async () => {
    const h = harness({ fetch: (url, opt, response) => {
      if (url.endsWith('/finish')) return Promise.resolve(response({ code: 0, data: { phase: 'running' } }));
      if (url.includes('/finish-status?')) {
        if (!malformed) return Promise.reject(new Error('poll connection unavailable'));
        return Promise.resolve({ ok: false, status: 502, json: async () => { throw new SyntaxError('HTML gateway error'); } });
      }
    } });
    completedPlan(h); h.runUploadActive(); await flush();
    for (let i = 0; i < 15; i++) { h.clock.tick(1000); await flush(); }
    assert.equal(h.state.running, false);
    assert.equal(h.nodes.get('runPhase').textContent, '异常中断');
    assert.equal(h.requests.filter(r => r.url.includes('/finish-status?')).length, 3);
    assert.equal(h.timers.size, 0);
  });
}

test('reopened receipt bounds consecutive transport failures without re-admitting work', async () => {
  let polls = 0;
  const h = harness({ fetch: (url, opt, response) => {
    if (url.endsWith('/finish-retry')) return Promise.resolve(response({ code: 0, data: { phase: 'running' } }));
    if (url.includes('/finish-status?')) {
      if (++polls === 1) return Promise.resolve(response({ code: 0, data: { phase: 'running' } }));
      return Promise.reject(new Error('poll connection unavailable'));
    }
  } });
  const reopened = { ...job, finish_pending: 'b'.repeat(32) };
  h.state.job = reopened;
  let error;
  const p = h.readResume(reopened).catch(e => { error = e; });
  for (let i = 0; i < 15; i++) { h.clock.tick(1000); await flush(); }
  assert.ok(error);
  assert.equal(h.requests.filter(r => r.url.endsWith('/finish-retry')).length, 0);
  assert.equal(polls, 4);
  assert.equal(h.timers.size, 0);
  await p;
});

for (const finishing of [false, true]) {
  test('50000-file reopened verification survives intermittent network failures: ' + finishing, async () => {
    let calls = 0;
    const info = { run_id: 'saved', files: [], plan: { previous_uploaded: 50000 } };
    const h = harness({ fetch: (url, opt, response) => {
      if (url.includes('/resume?') || url.includes('/finish-status?') || url.endsWith('/finish-retry')) {
        calls++;
        if ([2, 4, 6].includes(calls)) return Promise.reject(new Error('temporary network failure'));
        return Promise.resolve(response({ code: 0, data: { request_id: 'b'.repeat(32),
          phase: calls < 7 ? 'running' : 'done',
          result: calls >= 7 ? (finishing ? { status: 'ok' } : info) : undefined } }));
      }
    } });
    const reopened = { ...job, ...(finishing ? { finish_pending: 'b'.repeat(32) }
      : { resume_check_pending: 'b'.repeat(32) }) };
    h.state.job = reopened;
    let result, error;
    const p = h.readResume(reopened).then(out => { result = out; }, e => { error = e; });
    for (let i = 0; i < 12; i++) { h.clock.tick(1000); await flush(); }
    await p;
    assert.equal(error, undefined);
    if (finishing) assert.equal(result.finish_result.status, 'ok');
    else assert.deepEqual(result, info);
    assert.equal(calls, 7);
    assert.equal(h.timers.size, 0);
  });
}

for (const resume of [false, true]) {
  test('healthy status polls reset transient server failure budget: ' + resume, async () => {
    let calls = 0;
    const info = { run_id: 'saved', files: [], plan: { previous_uploaded: 50000 } };
    const h = harness({ fetch: (url, opt, response) => {
      if (url.endsWith('/finish') || url.includes('/finish-status?') || url.includes('/resume?')) {
        calls++;
        if ([2, 5, 8].includes(calls)) return Promise.resolve({ ok: false, status: 500,
          json: async () => ({ code: 500, message: 'temporary server failure' }) });
        return Promise.resolve(response({ code: 0, data: { request_id: 'b'.repeat(32),
          phase: calls < 11 ? 'running' : 'done', result: calls >= 11 ? (resume ? info : { status: 'ok' }) : undefined } }));
      }
    } });
    let result, error, p;
    if (resume) {
      h.state.job = job;
      p = h.readResume(job).then(out => { result = out; }, e => { error = e; });
    } else { completedPlan(h); h.runUploadActive(); }
    for (let i = 0; i < 18; i++) { h.clock.tick(1000); await flush(); }
    if (resume) { await p; assert.equal(error, undefined); assert.deepEqual(result, info); }
    else assert.equal(h.nodes.get('runPhase').textContent, '完成');
    assert.equal(calls, 11);
    assert.equal(h.timers.size, 0);
  });
}

test('50000 completed files wait through 89 seconds of finish verification before showing success', async () => {
  let done = false;
  const h = harness({ fetch: (url, opt, response) => {
    if (url.endsWith('/finish') || url.includes('/finish-status?')) {
      const request_id = opt.body ? JSON.parse(opt.body).request_id : new URL(url, 'http://local').searchParams.get('request_id');
      return Promise.resolve(response({ code: 0, data: { request_id, phase: done ? 'done' : 'running',
        result: done ? { status: 'ok' } : undefined } }));
    }
  } });
  completedPlan(h); h.runUploadActive(); await flush();
  assert.equal(h.nodes.get('runPhase').textContent, '收尾校验中…');
  for (let i = 0; i < 89; i++) { h.clock.tick(1000); await flush(); }
  assert.equal(h.state.running, true);
  assert.equal(h.nodes.get('runPhase').textContent, '收尾校验中…');
  assert.equal(h.xhrs.length, 0);
  done = true; h.clock.tick(1000); await until(() => !h.state.running);
  assert.equal(h.nodes.get('runPhase').textContent, '完成');
  assert.equal(h.requests.filter(r => r.url.endsWith('/finish')).length, 1);
  assert.equal(h.timers.size, 0);
});

test('finish lost admission ACK retries the same identity and stats after timeout', async () => {
  let calls = 0;
  const h = harness({ fetch: (url, opt, response) => {
    if (url.endsWith('/finish')) {
      if (++calls === 1) return new Promise(() => {});
      return Promise.resolve(response({ code: 0, data: { phase: 'done', result: { status: 'ok' } } }));
    }
  } });
  completedPlan(h); h.runUploadActive(); await flush();
  h.clock.tick(60000); await flush(); h.clock.tick(1000); await flush();
  assert.equal(calls, 2);
  assert.equal(h.state.running, false);
  const posts = h.requests.filter(r => r.url.endsWith('/finish'));
  assert.equal(posts[0].opt.body, posts[1].opt.body);
  assert.equal(h.nodes.get('runPhase').textContent, '完成');
  assert.equal(h.timers.size, 0);
});

test('cancellation during finish polling waits for the authoritative cancelled receipt', async () => {
  let cancelled = false;
  const h = harness({ fetch: (url, opt, response) => {
    if (url.endsWith('/finish-cancel')) cancelled = true;
    if (url.endsWith('/finish') || url.includes('/finish-status?') || url.endsWith('/finish-cancel')) {
      return Promise.resolve(response({ code: 0, data: { phase: cancelled ? 'done' : 'running',
        result: cancelled ? { status: 'cancelled', cancelled: true } : undefined } }));
    }
  } });
  completedPlan(h); h.runUploadActive(); await flush();
  h.state.cancel = true; h.abortAllUploads(); h.clock.tick(1000); await until(() => !h.state.running);
  assert.equal(cancelled, true);
  assert.equal(h.nodes.get('runPhase').textContent, '已取消');
  assert.equal(h.xhrs.length, 0);
  assert.equal(h.timers.size, 0);
});

test('50000-file resume verification survives 89 seconds without a long HTTP request', async () => {
  let done = false;
  const info = { run_id: 'saved', files: [], plan: { previous_uploaded: 50000 } };
  const h = harness({ fetch: (url, opt, response) => {
    if (url.includes('/resume?')) {
      const request_id = new URL(url, 'http://local').searchParams.get('request_id');
      return Promise.resolve(response({ code: 0, data: { request_id,
        phase: done ? 'done' : 'running', result: done ? info : undefined } }));
    }
  } });
  h.state.job = job;
  let settled = false;
  const pending = h.readResume(job).then(out => { settled = true; return out; });
  await flush();
  for (let i = 0; i < 89; i++) { h.clock.tick(1000); await flush(); }
  assert.equal(settled, false);
  done = true; h.clock.tick(1000);
  assert.deepEqual(await pending, info);
  assert.equal(h.timers.size, 0);
  assert.equal(h.xhrs.length, 0);
});

test('reopening a finishing job queries its original receipt without re-upload or new finish', async () => {
  const h = harness({ fetch: (url, opt, response) => {
    if (url.includes('/finish-status?')) return Promise.resolve(response({ code: 0,
      data: { phase: 'done', result: { status: 'ok' } } }));
  } });
  const reopened = { ...job, finish_pending: 'b'.repeat(32) };
  h.state.job = reopened;
  assert.equal((await h.readResume(reopened)).finish_result.status, 'ok');
  assert.equal(h.requests.length, 1);
  assert.ok(h.requests[0].url.endsWith('/finish-status?request_id=' + reopened.finish_pending));
  assert.equal(h.timers.size, 0);
});

test('permanent finish errors stop after bounded retries and never announce success', async () => {
  let attempts = 0;
  const h = harness({ fetch: (url, opt, response) => {
    if (url.endsWith('/finish')) {
      attempts++;
      return Promise.resolve(response({ code: 0, data: { phase: 'running' } }));
    }
    if (url.includes('/finish-status?')) return Promise.resolve({ ok: false, status: 500,
      json: async () => ({ code: 500, message: 'disk unavailable' }) });
  } });
  completedPlan(h); h.runUploadActive(); await flush();
  for (let i = 0; i < 10; i++) { h.clock.tick(1000); await flush(); }
  assert.equal(h.state.running, false);
  assert.equal(attempts, 3);
  assert.equal(h.nodes.get('runPhase').textContent, '异常中断');
  assert.ok(!h.nodes.get('runLog').childNodes.some(n => n.textContent === '[阶段] 完成'));
  assert.equal(h.timers.size, 0);
});

test('reopening a partial browser finish without a checkpoint preserves the partial result', async () => {
  const h = harness({ fetch: (url, opt, response) => {
    if (url.includes('/finish-status?')) return Promise.resolve(response({ code: 0,
      data: { phase: 'done', result: { status: 'partial' } } }));
    if (url.includes('/resume?')) return Promise.resolve(response({ code: 0,
      data: { phase: 'done', result: null } }));
  } });
  const reopened = { ...job, finish_pending: 'b'.repeat(32) };
  h.state.job = reopened;
  assert.equal((await h.readResume(reopened)).finish_result.status, 'partial');
  assert.equal(h.timers.size, 0);
});

test('a cancellation arriving after the durable success receipt still reports that success', async () => {
  const h = harness({ fetch: (url, opt, response) => {
    if (url.endsWith('/finish')) return Promise.resolve(response({ code: 0, data: { phase: 'running' } }));
    if (url.endsWith('/finish-cancel')) return Promise.resolve(response({ code: 0,
      data: { phase: 'done', result: { status: 'ok', cancelled: false } } }));
  } });
  completedPlan(h); h.runUploadActive(); await flush();
  h.state.cancel = true; h.clock.tick(1000); await until(() => !h.state.running);
  assert.equal(h.state.cancel, false);
  assert.equal(h.nodes.get('runPhase').textContent, '完成');
  assert.equal(h.timers.size, 0);
});

test('reopened page recovers a failed receipt with its persisted identity and no original stats', async () => {
  let retried = false;
  const h = harness({ fetch: (url, opt, response) => {
    if (url.endsWith('/finish-retry')) {
      assert.equal(opt.method, 'POST');
      assert.deepEqual(JSON.parse(opt.body), { request_id: 'b'.repeat(32) });
      retried = true;
      return Promise.resolve(response({ code: 0, data: { phase: 'running' } }));
    }
    if (url.includes('/finish-status?')) {
      if (!retried) return Promise.resolve({ ok: false, status: 500,
        json: async () => ({ code: 500, message: 'receipt write failed' }) });
      return Promise.resolve(response({ code: 0, data: { phase: 'done', result: { status: 'ok' } } }));
    }
  } });
  const reopened = { ...job, finish_pending: 'b'.repeat(32) };
  h.state.job = reopened;
  let result, error;
  const p = h.readResume(reopened).then(out => { result = out; }, e => { error = e; });
  for (let i = 0; i < 5; i++) { await flush(); h.clock.tick(1000); }
  await flush();
  assert.equal(retried, true);
  assert.equal(error, undefined);
  assert.equal(result.finish_result.status, 'ok');
  assert.equal(h.requests.filter(r => r.url.endsWith('/finish')).length, 0);
  assert.equal(h.timers.size, 0);
  await p;
});

test('reopened page bounds permanently failing receipt recovery attempts', async () => {
  let retries = 0;
  const h = harness({ fetch: (url, opt, response) => {
    if (url.endsWith('/finish-retry')) {
      retries++;
      return Promise.resolve(response({ code: 0, data: { phase: 'running' } }));
    }
    if (url.includes('/finish-status?')) return Promise.resolve({ ok: false, status: 500,
      json: async () => ({ code: 500, message: 'receipt unavailable' }) });
  } });
  const reopened = { ...job, finish_pending: 'b'.repeat(32) };
  h.state.job = reopened;
  let error;
  const p = h.readResume(reopened).catch(e => { error = e; });
  for (let i = 0; i < 12; i++) { h.clock.tick(1000); await flush(); }
  assert.equal(retries, 2);
  assert.equal(error.status, 500);
  assert.equal(h.timers.size, 0);
  await p;
});

test('permanent resume verification errors stop even when every retry admission succeeds', async () => {
  let calls = 0;
  const h = harness({ fetch: (url, opt, response) => {
    if (url.includes('/resume?')) {
      if (++calls % 2 === 0) return Promise.resolve({ ok: false, status: 500,
        json: async () => ({ code: 500, message: 'read unavailable' }) });
      const request_id = new URL(url, 'http://local').searchParams.get('request_id');
      return Promise.resolve(response({ code: 0, data: { request_id, phase: 'running' } }));
    }
  } });
  h.state.job = job;
  let error;
  const p = h.readResume(job).catch(e => { error = e; });
  for (let i = 0; i < 12; i++) { h.clock.tick(1000); await flush(); }
  assert.ok(error, 'a healthy admission must not reset the failed-work retry budget');
  assert.equal(error.status, 500);
  assert.equal(calls, 6);
  assert.equal(h.timers.size, 0);
  await p;
});

test('a failed resume verification retries the same identity and returns saved progress', async () => {
  let calls = 0;
  const ids = [];
  const info = { run_id: 'saved', files: [['partial.txt', 5, 1000]],
    plan: { previous_uploaded: 49999 } };
  const h = harness({ fetch: (url, opt, response) => {
    if (url.includes('/resume?')) {
      const request_id = new URL(url, 'http://local').searchParams.get('request_id');
      ids.push(request_id);
      if (++calls === 2) return Promise.resolve({ ok: false, status: 500,
        json: async () => ({ code: 500, message: 'transient read failure' }) });
      return Promise.resolve(response({ code: 0, data: { request_id,
        phase: calls < 4 ? 'running' : 'done', result: calls >= 4 ? info : undefined } }));
    }
  } });
  h.state.job = job;
  const p = h.readResume(job);
  for (let i = 0; i < 5; i++) { h.clock.tick(1000); await flush(); }
  assert.deepEqual(await p, info);
  assert.equal(new Set(ids).size, 1);
  assert.equal(calls, 4);
  assert.equal(h.timers.size, 0);
});
