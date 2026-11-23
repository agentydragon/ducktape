import { strict as assert } from "node:assert";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { lowerDiagnostic, prepareSideSplit, run } from "./testing/diagnostic_split.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const good = `function factory(f) { return async (...args) => f(...args); }
function callback() { console.log("callback"); }
const result = factory(callback);
console.log("side");
result();`;
function emit(name, code, selectedSide, force = false) {
  const fixture = prepareSideSplit(name, code, selectedSide);
  const result = lowerDiagnostic(
    fixture,
    "--lean",
    path.join(fixture.dir, "certificate.lean"),
    ...(force ? ["--force"] : [])
  );
  return { ...fixture, result };
}
const goodResult = emit("good", good);
assert.equal(goodResult.result.status, 0, `${goodResult.result.stdout}\n${goodResult.result.stderr}`);
const before = run(process.execPath, [goodResult.source]);
const after = run(process.execPath, [path.join(goodResult.dir, "entry.mjs")]);
assert.equal(before.status, 0, before.stderr);
assert.equal(after.status, 0, after.stderr);
assert.equal(after.stdout, before.stdout);
assert.equal(after.stdout, "side\ncallback\n");
const frontend = path.join(here, "frontend.mjs");
const bundle = path.join(goodResult.dir, "bundle.json");
const tree = path.join(goodResult.dir, "tree.json");
const cert = path.join(goodResult.dir, "cert.json");
for (const args of [
  [frontend, "bundle", goodResult.source, "--out", bundle],
  [frontend, "tree", goodResult.dir, "--entry", "entry.mjs", "--out", tree],
  [path.join(here, "pair.mjs"), bundle, tree, "--out", cert],
]) {
  const r = run(process.execPath, args);
  assert.equal(r.status, 0, r.stderr);
}
const checked = run(process.env.TRANSLATE, [bundle, tree, cert]);
assert.equal(checked.status, 0, `${checked.stdout}\n${checked.stderr}`);
const multi = good
  .replace("const result = factory(callback);", "const result = factory(callback);\nconst result2 = factory(callback);")
  .replace("result();", "void (result(), result2());");
const multiResult = emit("multi", multi);
assert.equal(multiResult.result.status, 0, `${multiResult.result.stdout}\n${multiResult.result.stderr}`);
const multiBefore = run(process.execPath, [multiResult.source]);
const multiAfter = run(process.execPath, [path.join(multiResult.dir, "entry.mjs")]);
assert.equal(multiBefore.status, 0, multiBefore.stderr);
assert.equal(multiAfter.status, 0, multiAfter.stderr);
assert.equal(multiAfter.stdout, multiBefore.stdout);
assert.equal(multiAfter.stdout, "side\ncallback\ncallback\n");
// An external observer receives the actual fresh closure and invokes it later.
const escape = `globalThis.capture = f => { globalThis.saved = f; };\n${good.replace("result();", "globalThis.capture(result);\nglobalThis.saved();")}`;
const escapeResult = emit("escape", escape, [0, 4]);
assert.equal(escapeResult.result.status, 0, `${escapeResult.result.stdout}\n${escapeResult.result.stderr}`);
const escapeBefore = run(process.execPath, [escapeResult.source]);
const escapeAfter = run(process.execPath, [path.join(escapeResult.dir, "entry.mjs")]);
assert.equal(escapeBefore.status, 0, escapeBefore.stderr);
assert.equal(escapeAfter.status, 0, escapeAfter.stderr);
assert.equal(escapeAfter.stdout, escapeBefore.stdout);
assert.equal(escapeAfter.stdout, "side\ncallback\n");
// Moving an observer/call before the result exists must be rejected.
for (const observer of ["result();", "globalThis.observe(result);"]) {
  const bad = good.replace('console.log("side");', observer);
  const { dir, result } = emit(`observer${observer.length}`, bad);
  assert.notEqual(result.status, 0, `${observer}: ${result.stdout}\n${result.stderr}`);
  assert(!existsSync(path.join(dir, "entry.mjs")), observer);
}
// Moving the side statement before the factory call changes the output when the call or the factory can print.
for (const [name, bad] of [
  ["body", good.replace("return async", 'console.log("body"); return async')],
  ["argument", good.replace("factory(callback)", 'factory((console.log("argument"), callback))')],
  ["reassign", good.replace("const result", 'factory = f => { console.log("changed"); return f; };\nconst result')],
  [
    "eager_callback",
    good.replace("return async (...args) => f(...args)", "return (f(), async (...args) => f(...args))"),
  ],
  [
    "returned_eager_call",
    good.replace(
      "return async (...args) => f(...args)",
      'return (console.log("eager"), async (...args) => f(...args))'
    ),
  ],
]) {
  const forcedVariant = emit(`${name}-forced`, bad, undefined, true);
  const original = run(process.execPath, [forcedVariant.source]);
  const split = run(process.execPath, [path.join(forcedVariant.dir, "entry.mjs")]);
  assert.notEqual(split.stdout, original.stdout, `${name}: the split should change the output`);
  const { dir, result } = emit(name, bad);
  assert.equal(result.status, 1, `${name}: ${result.stdout}\n${result.stderr}`);
  assert.match(result.stdout, /FAIL\s+OrderOk/, name);
  assert(!existsSync(path.join(dir, "entry.mjs")), name);
}

// A callback saved before the factory call may reach its result later through
// host state, a module-local mutable cell, or an inert alias.
const base = `function callback() {}
function factory(f) { return () => f(); }
`;
for (const [name, code, side] of [
  [
    "deferred_result",
    `${base}globalThis.cb = () => result();
const result = factory(callback);
globalThis.cb();`,
    [2, 4],
  ],
  [
    "deferred_alias",
    `${base}globalThis.cb = () => alias();
const result = factory(callback);
const alias = result;
globalThis.cb();`,
    [2, 5],
  ],
  [
    "deferred_object_alias",
    `${base}globalThis.cb = () => alias.value();
const result = factory(callback);
const alias = {value: result};
globalThis.cb();`,
    [2, 5],
  ],
  [
    "instance_field",
    `${base}globalThis.Klass = class { x = result(); };
const result = factory(callback);
new globalThis.Klass();`,
    [2, 4],
  ],
  [
    "mutable_slot",
    `let slot;
function register(f) { slot = f; }
function trigger() { slot(); }
function callback() {}
function factory(f) { return () => f(); }
function later() { result(); }
register(later);
const result = factory(callback);
trigger();`,
    [0, 1, 2, 6, 8],
  ],
]) {
  const rejected = emit(name, code, side);
  assert.equal(rejected.result.status, 1, `${name}: ${rejected.result.stdout}\n${rejected.result.stderr}`);
  assert.match(rejected.result.stdout, /FAIL\s+OrderOk/, name);
  assert(!existsSync(path.join(rejected.dir, "entry.mjs")), name);
  const forced = emit(`${name}-forced`, code, side, true);
  assert.equal(forced.result.status, 1, name);
  const original = run(process.execPath, [forced.source]);
  const emitted = run(process.execPath, [path.join(forced.dir, "entry.mjs")]);
  assert.equal(original.status, 0, original.stderr);
  assert.notEqual(emitted.status, 0, name);
  assert.match(emitted.stderr, /ReferenceError/, name);
  const originalBundle = path.join(process.env.TEST_TMPDIR, `${name}-bundle.json`);
  const outputTree = path.join(process.env.TEST_TMPDIR, `${name}-tree.json`);
  const pairing = path.join(process.env.TEST_TMPDIR, `${name}-pairing.json`);
  for (const args of [
    [frontend, "bundle", forced.source, "--out", originalBundle],
    [frontend, "tree", forced.dir, "--entry", "entry.mjs", "--out", outputTree],
    [path.join(here, "pair.mjs"), originalBundle, outputTree, "--out", pairing],
  ]) {
    const r = run(process.execPath, args);
    assert.equal(r.status, 0, r.stderr);
  }
  const checkedForced = run(process.env.TRANSLATE, [originalBundle, outputTree, pairing]);
  assert.equal(checkedForced.status, 4, `${name}: ${checkedForced.stdout}\n${checkedForced.stderr}`);
  assert.match(checkedForced.stdout, /FAIL\s+OrderOk/, name);
}

// A fallback syntax node may hide deferred behavior; the statement is
// reported with its fallback nodes.
{
  const code = `function factory(f) { return () => f(); }
function callback() {}
const result = factory(callback);
console.log(import.meta.url, result);`;
  const result = emit("fallback_factory", code, []);
  assert.equal(result.result.status, 0, `${result.result.stdout}\n${result.result.stderr}`);
  assert.match(result.result.stdout, /1 with fallback nodes/);
}
