const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "xiaomi-nas-plugin-pcbackup", "src", "ui", "app.js"), "utf8"
);
const start = source.indexOf("  function toBytes(");
const end = source.indexOf("  // 原生读取入口", start);
assert.ok(start >= 0 && end > start, "native reader code is present");

function reader(nodeRequire) {
  const context = vm.createContext({
    Promise, Blob, ArrayBuffer, Uint8Array, setTimeout, clearTimeout,
    BRIDGE_MAX_FILE: 64 * 1024 * 1024,
    require: nodeRequire || function () { throw new Error("Node fs unavailable"); },
    window: {}
  });
  vm.runInContext(source.slice(start, end), context);
  return context.readPcChunk;
}

function bridgeFor(bytes) {
  return {
    getPcFileStream(filePath, start, onData, onEnd, onError) {
      if (typeof onData !== "function") {
        queueMicrotask(function () { onError(new TypeError("data callback must be a function")); });
        return;
      }
      queueMicrotask(function () {
        onData(Uint8Array.from(bytes.slice(start)));
        onEnd();
      });
    }
  };
}

test("desktop bridge returns the requested bytes from a file stream", async function () {
  const blob = await reader()(bridgeFor([1, 2, 3, 4]), "D:\\work\\a.txt", 0, 4, 4);
  assert.deepEqual(Array.from(new Uint8Array(await blob.arrayBuffer())), [1, 2, 3, 4]);
});

test("desktop bridge keeps the requested offset and trims later bytes", async function () {
  const blob = await reader()(bridgeFor([1, 2, 3, 4, 5, 6]), "D:\\work\\a.txt", 2, 3, 6);
  assert.deepEqual(Array.from(new Uint8Array(await blob.arrayBuffer())), [3, 4, 5]);
});

test("direct Node reads use exact offsets when available", async function () {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pcbackup-reader-"));
  const file = path.join(dir, "sample.bin");
  fs.writeFileSync(file, Buffer.from([10, 20, 30, 40, 50]));
  try {
    const blob = await reader(require)(bridgeFor([]), file, 1, 3, 5);
    assert.deepEqual(Array.from(new Uint8Array(await blob.arrayBuffer())), [20, 30, 40]);
  } finally {
    fs.unlinkSync(file);
    fs.rmdirSync(dir);
  }
});

test("bridge reads are serialized because its IPC events have no file identifier", async function () {
  let active = 0, peak = 0;
  const bridge = {
    getPcFileStream(filePath, start, onData, onEnd) {
      active++;
      peak = Math.max(peak, active);
      setTimeout(function () {
        onData(Uint8Array.from([filePath === "first" ? 1 : 2]));
        active--;
        onEnd();
      }, 5);
    }
  };
  const read = reader();
  const blobs = await Promise.all([
    read(bridge, "first", 0, 1, 1),
    read(bridge, "second", 0, 1, 1)
  ]);
  assert.equal(peak, 1);
  assert.deepEqual(await Promise.all(blobs.map(async function (blob) {
    return Array.from(new Uint8Array(await blob.arrayBuffer()));
  })), [[1], [2]]);
});

test("bridge rejects an incomplete stream instead of uploading a short chunk", async function () {
  const bridge = {
    getPcFileStream(filePath, start, onData, onEnd) {
      queueMicrotask(function () { onData(Uint8Array.from([1])); onEnd(); });
    }
  };
  await assert.rejects(reader()(bridge, "short", 0, 2, 2), /分块长度不符/);
});

test("bridge reports native read errors and requires browser mode for large files", async function () {
  const bridge = {
    getPcFileStream(filePath, start, onData, onEnd, onError) {
      queueMicrotask(function () { onError(new Error("access denied")); });
    }
  };
  const read = reader();
  await assert.rejects(read(bridge, "unreadable", 0, 1, 1), /access denied/);
  await assert.rejects(read(bridge, "large", 0, 1, 65 * 1024 * 1024), /选择文件夹/);
});
