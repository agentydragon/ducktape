import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [source] = process.argv.slice(2);
const text = readFileSync(source, "utf8");
assert.equal(
  createHash("sha256").update(text).digest("hex"),
  "9a1ab71f40d915f55f3985c314a844d7686f936b1a097dd78ded2c92cf84ba44"
);
const ast = JSON.parse(readFileSync(source.slice(0, -4) + ".json", "utf8"));
const [first, second] = ast.body.slice(0, 2).map(({ start, end }) => text.slice(start, end));
assert.deepEqual([first, second], ["var __create = Object.create;", "var __defProp = Object.defineProperty;"]);
const root = process.env.TEST_TMPDIR;
const prefix = path.join(root, "prefix.mjs");
const emitted = path.join(root, "emitted");
writeFileSync(prefix, [first, second, ""].join("\n"));
const model = execFileSync(process.env.SEMVER_BOOTSTRAP, [source, emitted], { encoding: "utf8" });
console.log(model.trim());
const readonlyScript = `
import assert from "node:assert/strict";
const [file, mode] = process.argv.slice(1);
const O = globalThis.Object;
const trace = [];
const sentinel = {};
const saved = {};
const base = {
  get create() {
    assert.equal(this, base);
    trace.push("C");
    if (mode === "property-throw") throw sentinel;
    return saved;
  },
  get defineProperty() { assert.equal(this, base); trace.push("D"); return 5; },
};
O.defineProperty(globalThis, "Object", {
  configurable: true,
  get() {
    assert.equal(this, globalThis);
    trace.push("B");
    if (mode === "binding-throw") throw sentinel;
    return base;
  },
});
let threw = false;
try { await import(file); }
catch (error) { assert.equal(error, sentinel); threw = true; }
finally { O.defineProperty(globalThis, "Object", {value: O, writable: true, configurable: true}); }
console.log(JSON.stringify([trace.join(""), threw]));
`;
function observe(file, mode) {
  return JSON.parse(
    execFileSync(process.execPath, ["--input-type=module", "-e", readonlyScript, pathToFileURL(file).href, mode], {
      encoding: "utf8",
    })
  );
}
for (const file of [prefix, path.join(emitted, "entry.mjs")]) {
  assert.deepEqual(observe(file, "normal"), ["BCBD", false]);
  assert.deepEqual(observe(file, "binding-throw"), ["B", true]);
  assert.deepEqual(observe(file, "property-throw"), ["BC", true]);
}
for (const [name, rows, trace] of [
  ["duplicated", [first, first, second], "BCBCBD"],
  ["reordered", [second, first], "BDBC"],
]) {
  const file = path.join(root, name + ".mjs");
  writeFileSync(file, rows.join("\n"));
  assert.deepEqual(observe(file, "normal"), [trace, false]);
}
const helper = path.join(emitted, "helper.mjs");
const helperCode = readFileSync(helper, "utf8");
const runRecheck = () =>
  spawnSync(process.env.SEMVER_BOOTSTRAP, [source, emitted, "--check-scratch"], { encoding: "utf8" });
for (const extra of [first, second]) {
  const changed = helperCode + "\n" + extra + "\n";
  writeFileSync(helper, changed);
  const result = runRecheck();
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.match(result.stderr, /output module helper\.mjs differs from its generated ESTree/);
  assert.equal(readFileSync(helper, "utf8"), changed);
}
writeFileSync(helper, helperCode);
const checked = runRecheck();
assert.equal(checked.status, 0, checked.stdout + checked.stderr);

const captureScript = `
import assert from "node:assert/strict";
const original = globalThis.Object;
const saved = {};
const old = { get create() { assert.equal(this, old); return saved; } };
globalThis.Object = old;
${first}
globalThis.Object = { create: "replaced" };
assert.equal(__create, saved);
assert.equal(Object.create, "replaced");
globalThis.Object = original;
`;
execFileSync(process.execPath, ["--input-type=module", "-e", captureScript], { encoding: "utf8" });
