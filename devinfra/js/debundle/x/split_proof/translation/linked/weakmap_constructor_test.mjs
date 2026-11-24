import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { forcePreview, lower, prepare, root } from "./harness.mjs";

const profile = path.join(root, "browser-page.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const opts = ["--host-contract", profile];

function fixture(name, crossed, init = "new WeakMap()") {
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source: `${crossed}; var cache = ${init}; export {cache};`,
    provider: "",
    spec: { entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#2"], "owner.mjs": ["#1"] } },
  });
  return { dir, files, out: path.join(dir, "out") };
}

const neutral = fixture("neutral", "var phase = 1");
for (const [scope, flags, reason] of [
  ["linked-structural", ["--structural"], /unit 1 is not a local allocating declaration/],
  ["linked-programs", [], /host value window\.WeakMap is constructed/],
]) {
  const { verdict } = lower(neutral.files.sourceFile, neutral.files.specFile, neutral.out, [...opts, ...flags], scope);
  assert.equal(verdict.status, "rejected");
  assert.equal(verdict.guarantee, null);
  assert.match(verdict.reason, reason);
  assert(!existsSync(neutral.out));
}

// No property or prototype tag encodes [[WeakMapData]]. The inherited brand
// string and instanceof both accept this ordinary fake. A native method does not.
const native = new WeakMap();
const other = new WeakMap();
const key = {};
const fake = Object.create(WeakMap.prototype);
assert.notEqual(native, other);
assert.equal(Object.getPrototypeOf(native), Object.getPrototypeOf(fake));
assert.deepEqual(Reflect.ownKeys(native), Reflect.ownKeys(fake));
assert(fake instanceof WeakMap);
assert.equal(Object.prototype.toString.call(native), Object.prototype.toString.call(fake));
Object.setPrototypeOf(native, null);
Object.setPrototypeOf(fake, null);
const { set, get, has } = WeakMap.prototype;
assert.equal(has.call(native, key), false);
assert.throws(() => has.call(fake, key), TypeError);
assert.equal(set.call(native, key, 7), native);
assert.equal(get.call(native, key), 7);
assert.equal(has.call(other, key), false);

// Inspect the original and the *uncertified* reordered preview in separate
// processes. The same stable replacement can return a shared object or reenter.
const observe = (file, mode) => {
  const probe = `
    const [file, mode] = process.argv.slice(1);
    const Native = WeakMap;
    globalThis.phase = 0;
    globalThis.sentinel = {};
    const log = [];
    if (mode === "getter") Object.defineProperty(globalThis, "WeakMap", {
      get() { log.push(phase); return Native; }, configurable: true
    });
    if (mode === "reentry") globalThis.WeakMap = class extends Native {
      constructor() { super(); log.push(phase); }
    };
    const { cache } = await import(file);
    const shared = cache === sentinel;
    console.log(JSON.stringify({ log, shared }));
  `;
  const run = spawnSync(process.execPath, ["--input-type=module", "-e", probe, pathToFileURL(file).href, mode], {
    encoding: "utf8",
  });
  assert.equal(run.status, 0, run.stderr);
  return JSON.parse(run.stdout);
};
const marker = fixture("phase", "globalThis.phase = 1");
const preview = path.join(marker.dir, "preview");
forcePreview(marker.files, preview);
for (const mode of ["getter", "reentry"]) {
  assert.deepEqual(observe(marker.files.sourceFile, mode), { log: [1], shared: false });
  assert.deepEqual(observe(path.join(preview, "entry.mjs"), mode), { log: [0], shared: false });
}

const sentinelCtor = "class { constructor() { return globalThis.sentinel; } }";
for (const [name, crossed] of [
  ["replaced", "globalThis.WeakMap = " + sentinelCtor],
  ["shadowed", "export const WeakMap = " + sentinelCtor],
]) {
  const test = fixture(name, crossed);
  const { verdict } = lower(
    test.files.sourceFile,
    test.files.specFile,
    test.out,
    ["--structural", ...opts],
    "linked-structural"
  );
  assert.equal(verdict.status, "rejected", JSON.stringify(verdict));
  assert(!existsSync(test.out));
  assert.deepEqual(observe(test.files.sourceFile, name), { log: [], shared: true });
  if (name === "replaced") {
    const forced = path.join(test.dir, "preview");
    forcePreview(test.files, forced);
    assert.deepEqual(observe(path.join(forced, "entry.mjs"), name), { log: [], shared: false });
  }
}
