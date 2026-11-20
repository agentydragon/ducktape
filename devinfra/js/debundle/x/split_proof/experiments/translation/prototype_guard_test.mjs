import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { diagnose } from "./prototype_guard.mjs";

const good = "class C { probe_x; constructor(o) { this.probe_x = o.probe_x; } } const result = new C({probe_x: 1});";
const missingProperty = good.replace("{probe_x: 1}", "{}");
const missingField = good.replace("probe_x; constructor", "constructor");
assert.deepEqual(diagnose(good), { candidate: true, reason: "closed-own-data" });
assert.equal(diagnose(missingProperty).reason, "argument-property-not-own");
assert.equal(diagnose(missingField).reason, "receiver-field-not-own");
const unknownCall = good.replace("const result", "unknown(); const result");
const tdz = "const result = new C({probe_x: 1}); class C { probe_x; constructor(o) { this.probe_x = o.probe_x; } }";
assert.equal(diagnose(unknownCall).reason, "closed-two-statement-module");
assert.equal(diagnose(tdz).reason, "local-class");
assert.equal(diagnose(good.replace("const result = new C", "const result = new Other")).reason, "known-constructor");
assert.equal(diagnose(good.replace("new C({probe_x: 1})", "new C(getOptions())")).reason, "literal-argument");
assert.equal(diagnose(good.replace("{probe_x: 1}", "{get probe_x() { return 1; }}")).reason, "ordinary-own-data");
assert.equal(diagnose(good.replace("class C {", "class C extends Object {")).reason, "local-class");
assert.equal(diagnose(good.replace("class C { probe_x;", "class C { probe_x; extra() {}")).reason, "class-shape");
assert.equal(diagnose(good.replace("this.probe_x = o.probe_x;", "unknown();")).reason, "constructor-statement");

function execute(program) {
  const setup = `const events = [];
globalThis.unknown = () => events.push("call");
Object.defineProperty(Object.prototype, "probe_x", {
  get() { events.push("get"); return 1; },
  set(v) { events.push("set"); }, configurable: true,
});\n`;
  const code = setup + program + "\nprocess.stdout.write(JSON.stringify([events, result.probe_x]));";
  return spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8" });
}
function run(program) {
  const p = execute(program);
  assert.equal(p.status, 0, p.stderr);
  return JSON.parse(p.stdout);
}
assert.deepEqual(run(good), [[], 1]);
assert.deepEqual(run(missingProperty), [["get"], 1]);
assert.deepEqual(run(missingField), [["set", "get"], 1]);
assert.deepEqual(run(unknownCall), [["call"], 1]);
const tdzResult = execute(tdz);
assert.notEqual(tdzResult.status, 0);
assert.match(tdzResult.stderr, /ReferenceError/);
