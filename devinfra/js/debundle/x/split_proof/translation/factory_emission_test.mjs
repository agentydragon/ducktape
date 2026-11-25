import { strict as assert } from "node:assert";
import { existsSync } from "node:fs";
import path from "node:path";
import { frontBundle } from "./frontend.mjs";
import { checkTranslation } from "./testing/check_translation.mjs";
import { prepareSideSplit, run } from "./testing/diagnostic_split.mjs";
import { split } from "./testing/split.mjs";

const good = `function factory(f) { return (...args) => f(...args); }
function callback() { console.log("callback"); }
const result = factory(callback);
console.log("side");
result();`;
function emit(name, code, selectedSide) {
  const fixture = prepareSideSplit(name, code, selectedSide);
  const result = split({
    ast: fixture.input,
    spec: fixture.spec,
    out: fixture.certifiedOut,
    hostContract: fixture.hostContract,
  });
  return { ...fixture, dir: fixture.certifiedOut, result };
}
function assertOutput({ result, source, dir }, expected) {
  assert.equal(result.status, "certified", result.log);
  const before = run(process.execPath, [source]);
  const after = run(process.execPath, [path.join(dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.equal(after.status, 0, after.stderr);
  assert.equal(after.stdout, before.stdout);
  assert.equal(after.stdout, expected);
}

const goodResult = emit("good", good);
assertOutput(goodResult, "side\ncallback\n");
const checked = checkTranslation(frontBundle(goodResult.source), goodResult.dir);
assert.equal(checked.status, 0, `${checked.stdout}\n${checked.stderr}`);
const certifiedRefusals = [];
{
  const asyncCode = good.replace("return (...args) => f(...args)", "return async (...args) => f(...args)");
  const { dir, result } = emit("async_closure", asyncCode);
  assert.equal(result.status, "refused", result.log);
  assert.equal(result.reasonCode, "probe_unknown", result.log);
  assert(result.reason, result.log);
  assert.equal(result.files.length, 0);
  assert(!existsSync(path.join(dir, "entry.mjs")));
  certifiedRefusals.push("async_closure");
}
const multi = good
  .replace("const result = factory(callback);", "const result = factory(callback);\nconst result2 = factory(callback);")
  .replace("result();", "void (result(), result2());");
const multiResult = emit("multi", multi);
assertOutput(multiResult, "side\ncallback\ncallback\n");
// An external observer receives the actual fresh closure and invokes it later.
const escape = `globalThis.capture = f => { globalThis.saved = f; };\n${good.replace("result();", "globalThis.capture(result);\nglobalThis.saved();")}`;
const escapeResult = emit("escape", escape, [0, 4]);
assert.equal(escapeResult.result.status, "refused", escapeResult.result.log);
assert.equal(escapeResult.result.reasonCode, "linked_input", escapeResult.result.log);
assert.match(escapeResult.result.reason, /unbound reference to globalThis/);
assert.deepEqual(escapeResult.result.files, []);
assert(!existsSync(path.join(escapeResult.dir, "entry.mjs")));
certifiedRefusals.push("escape");
// Moving an observer/call before the result exists must be rejected.
for (const observer of ["result();", "globalThis.observe(result);"]) {
  const bad = good.replace('console.log("side");', observer);
  const { dir, result } = emit(`observer${observer.length}`, bad);
  assert.equal(result.status, "refused", `${observer}: ${result.log}`);
  assert(result.reason, observer);
  assert.equal(result.files.length, 0);
  assert(!existsSync(path.join(dir, "entry.mjs")), observer);
}
// Moving the side statement before the factory call changes the output when the call or the factory can print.
for (const [name, bad] of [
  ["body", good.replace("return (...args) => f(...args)", 'console.log("body"); return (...args) => f(...args)')],
  ["argument", good.replace("factory(callback)", 'factory((console.log("argument"), callback))')],
  ["reassign", good.replace("const result", 'factory = f => { console.log("changed"); return f; };\nconst result')],
  ["eager_callback", good.replace("return (...args) => f(...args)", "return (f(), (...args) => f(...args))")],
  [
    "returned_eager_call",
    good.replace("return (...args) => f(...args)", 'return (console.log("eager"), (...args) => f(...args))'),
  ],
]) {
  const { dir, result } = emit(name, bad);
  assert.equal(result.status, "refused", `${name}: ${result.log}`);
  assert(result.reason, name);
  assert.equal(result.files.length, 0);
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
  assert.equal(rejected.result.status, "refused", `${name}: ${rejected.result.log}`);
  assert(rejected.result.reason, name);
  assert.equal(rejected.result.files.length, 0);
  assert(!existsSync(path.join(rejected.dir, "entry.mjs")), name);
}

// A fallback syntax node may hide deferred behavior; the certified lowering
// refuses it without publishing an unchecked preview.
{
  const code = `function factory(f) { return () => f(); }
function callback() {}
const result = factory(callback);
console.log(import.meta.url, result);`;
  const fixture = prepareSideSplit("fallback_factory", code, []);
  const result = split({
    ast: fixture.input,
    spec: fixture.spec,
    out: fixture.certifiedOut,
    hostContract: fixture.hostContract,
  });
  assert.equal(result.status, "refused", result.log);
  assert.equal(result.reasonCode, "linked_input");
  assert.equal(result.files.length, 0);
  certifiedRefusals.push("fallback_factory");
}
assert.deepEqual(certifiedRefusals, ["async_closure", "escape", "fallback_factory"]);
console.log("certified factory refusals:", JSON.stringify({ attempted: 3, refused: certifiedRefusals }));
