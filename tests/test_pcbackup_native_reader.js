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

test("direct Node reads use exact offsets when available", async function () {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pcbackup-reader-"));
  const file = path.join(dir, "sample.bin");
  fs.writeFileSync(file, Buffer.from([10, 20, 30, 40, 50]));
  try {
    const blob = await reader(require)(null, file, 1, 3, 5);
    assert.deepEqual(Array.from(new Uint8Array(await blob.arrayBuffer())), [20, 30, 40]);
  } finally {
    fs.unlinkSync(file);
    fs.rmdirSync(dir);
  }
});

test("bridge reads are rejected: getPcFileStream channel is disabled", async function () {
  const read = reader();
  await assert.rejects(read({}, "D:\\x\\a.txt", 0, 4, 4), /无法分块读取/);
});
