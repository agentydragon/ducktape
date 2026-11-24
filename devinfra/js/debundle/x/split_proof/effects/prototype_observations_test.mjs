// Node behavior for constructor reads and writes when the host patches Object.prototype.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";

const patch = (descriptor) => `Object.defineProperty(Object.prototype, "probe_x", { ${descriptor} });`;
const accessors = patch(`get() { events.push("get"); return 1; }, set() { events.push("set"); }`);
const inheritedData = patch("value: 42, writable: false");
const ctor = `class C {
  probe_x;
  callback;
  constructor(o) { this.probe_x = o.probe_x; this.callback = o.callback; }
}`;
const helper = "function make(o) { return new C(o); }";
const direct = 'const result = new C({ probe_x: 1, callback: () => events.push("callback") });';
const viaHelper = direct.replace("new C", "make");
const missingArgumentProperty = (init) => init.replace("probe_x: 1, ", "");
const noInstanceField = ctor.replace("probe_x;", "");
const eagerHelper = helper.replace("return new C(o)", "return new C((o.callback(), o))");

function execute(...statements) {
  const source = [
    "const events = [];",
    ...statements,
    "const value = result.probe_x;",
    "process.stdout.write(JSON.stringify([events, value]));",
  ].join("\n");
  return spawnSync(process.execPath, ["--input-type=module", "-e", source], { encoding: "utf8" });
}

function observed(...statements) {
  const result = execute(...statements);
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

for (const [setup, init] of [
  [[], direct],
  [[helper], viaHelper],
]) {
  assert.deepEqual(observed(accessors, ctor, ...setup, init), [[], 1]);
  assert.deepEqual(observed(accessors, ctor, ...setup, missingArgumentProperty(init)), [["get"], 1]);
  assert.deepEqual(observed(accessors, noInstanceField, ...setup, init), [["set", "get"], 1]);
  const tdz = execute(accessors, ...setup, init, ctor);
  assert.notEqual(tdz.status, 0);
  assert.match(tdz.stderr, /ReferenceError/);
}

assert.deepEqual(observed(accessors, ctor, eagerHelper, viaHelper), [["callback"], 1]);
assert.deepEqual(observed(ctor, helper, missingArgumentProperty(viaHelper), accessors), [[], null]);
assert.deepEqual(observed(inheritedData, ctor, helper, missingArgumentProperty(viaHelper)), [[], 42]);
const readOnly = execute(inheritedData, noInstanceField, helper, viaHelper);
assert.notEqual(readOnly.status, 0);
assert.match(readOnly.stderr, /TypeError/);
