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
assert.equal(diagnose(unknownCall).reason, "helper-shape");
assert.equal(diagnose(tdz).reason, "local-class");
assert.equal(diagnose(good.replace("const result = new C", "const result = new Other")).reason, "known-constructor");
assert.equal(diagnose(good.replace("new C({probe_x: 1})", "new C(getOptions())")).reason, "literal-argument");
assert.equal(diagnose(good.replace("{probe_x: 1}", "{get probe_x() { return 1; }}")).reason, "ordinary-own-data");
assert.equal(diagnose(good.replace("class C {", "class C extends Object {")).reason, "local-class");
assert.equal(diagnose(good.replace("class C { probe_x;", "class C { probe_x; extra() {}")).reason, "class-shape");
assert.equal(diagnose(good.replace("this.probe_x = o.probe_x;", "unknown();")).reason, "constructor-statement");

const helperGood = `class C { probe_x; callback; constructor(o) { this.probe_x = o.probe_x; this.callback = o.callback; } }
function make(o) { return new C(o); }
const result = make({probe_x: 1, callback: () => events.push("callback")});`;
const helperGetter = helperGood.replace("probe_x: 1, ", "");
const helperSetter = helperGood.replace("probe_x; callback;", "callback;");
const helperCallback = helperGood.replace("return new C(o);", "o.callback(); return new C(o);");
const helperNestedCallback = helperGood.replace("new C(o)", "new C((o.callback(), o))");
const helperReassigned = helperGood.replace("const result", "make = unknown; const result");
const classReassigned = helperGood.replace("const result", "C = unknown; const result");
const helperUnknown = helperGood.replace("const result", "unknown(); const result");
const helperShadowed = helperGood.replace(
  "function make(o) { return new C(o); }",
  "function make(C) { return new C(C); }"
);
const helperTdz = `const result = make({probe_x: 1}); class C { probe_x; constructor(o) { this.probe_x = o.probe_x; } } function make(o) { return new C(o); }`;
assert.deepEqual(diagnose(helperGood), { candidate: true, reason: "closed-own-data" });
assert.equal(diagnose(helperGetter).reason, "argument-property-not-own");
assert.equal(diagnose(helperSetter).reason, "receiver-field-not-own");
assert.equal(diagnose(helperCallback).reason, "helper-shape");
assert.equal(diagnose(helperNestedCallback).reason, "helper-forwarding");
assert.equal(diagnose(helperReassigned).reason, "closed-module");
assert.equal(diagnose(classReassigned).reason, "closed-module");
assert.equal(diagnose(helperUnknown).reason, "closed-module");
assert.equal(diagnose(helperShadowed).reason, "helper-shape");
assert.equal(diagnose(helperTdz).reason, "local-class");

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
assert.deepEqual(run(helperGood), [[], 1]);
assert.deepEqual(run(helperGetter), [["get"], 1]);
assert.deepEqual(run(helperSetter), [["set", "get"], 1]);
assert.deepEqual(run(helperCallback), [["callback"], 1]);
assert.deepEqual(run(helperNestedCallback), [["callback"], 1]);
assert.deepEqual(run(helperUnknown), [["call"], 1]);
const helperTdzResult = execute(helperTdz);
assert.notEqual(helperTdzResult.status, 0);
assert.match(helperTdzResult.stderr, /ReferenceError/);
// An inherited data property also matters: a read can see its value, and an
// assignment to a non-writable inherited property throws in a strict module.
function executeWithInheritedData(program) {
  const setup = `const events = [];
Object.defineProperty(Object.prototype, "probe_x", {value: 42, writable: false, configurable: true});\n`;
  const code = setup + program + "\nprocess.stdout.write(JSON.stringify(result.probe_x));";
  return spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8" });
}
const inheritedRead = executeWithInheritedData(helperGetter);
assert.equal(inheritedRead.status, 0, inheritedRead.stderr);
assert.equal(inheritedRead.stdout, "42");
const inheritedWrite = executeWithInheritedData(helperSetter);
assert.notEqual(inheritedWrite.status, 0);
assert.match(inheritedWrite.stderr, /TypeError/);
const tdzResult = execute(tdz);
assert.notEqual(tdzResult.status, 0);
assert.match(tdzResult.stderr, /ReferenceError/);
