// Splits that move a statement across something that can observe it. Accepted emission controls use the certified
// helper; the remaining diagnostic cases assert model verdict text until the linked checker has the needed strategies.
//
// - `refuse`: a callback or an exported binding can observe the move. Both checkers must refuse it without publishing
//   output.
// - `accept`: the move changes nothing. Both checkers must accept it and Node must print the same.
// - `byBehavior`: compare certified output with Node and count any certified refusals; keep model verdict checks for
//   cases the diagnostic checker itself refuses.
//
// Not covered, because the checker assumes it does not happen (premise P4: evaluation completes): a source that throws
// when it runs, such as one that reads a `const` before its declaration.
import { strict as assert } from "node:assert";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { frontBundle } from "./frontend.mjs";
import { checkTranslation } from "./testing/check_translation.mjs";
import { lowerDiagnostic, prepareSideSplit, run } from "./testing/diagnostic_split.mjs";
import { split } from "./testing/split.mjs";

const temp = process.env.TEST_TMPDIR;
assert(temp);
const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = (name) => readFileSync(path.join(here, "testdata/inert_escape", name), "utf8");
const certifiedControlRefusals = [];
const certifiedBehaviorRefusals = [];

const describe = (name, r) => `${name}: ${r.stdout}\n${r.stderr}`;

function refuse(name, code, side) {
  const p = prepareSideSplit(name, code, side);
  const rejected = lowerDiagnostic(p);
  assert.equal(rejected.status, 1, describe(name, rejected));
  assert.match(rejected.stdout, /FAIL\s+OrderOk/, name);
  assert.equal(rejected.certification.status, "refused", rejected.certification.log);
  assert(rejected.certification.reason, rejected.certification.log);
  assert.deepEqual(rejected.certification.files, []);
  assert(!existsSync(path.join(p.dir, "entry.mjs")), `${name} wrote modules after a rejected plan`);
  const before = run(process.execPath, [p.source]);
  assert.equal(before.status, 0, before.stderr);
}

function accept(name, code, side) {
  const p = prepareSideSplit(name, code, side);
  const emitted = split({
    ast: p.input,
    spec: p.spec,
    out: p.certifiedOut,
    hostContract: p.hostContract,
  });
  if (emitted.status === "refused") {
    certifiedControlRefusals.push(name);
    const expectedReasonCode = name === "literal_array" ? "linked_input" : "probe_unknown";
    assert(["literal_array", "private_array_includes_before_factory"].includes(name), emitted.log);
    assert.equal(emitted.reasonCode, expectedReasonCode, emitted.log);
    assert(emitted.reason, emitted.log);
    assert.deepEqual(emitted.files, []);
    return;
  }
  assert.equal(emitted.status, "certified", `${name}: ${emitted.log}`);
  const before = run(process.execPath, [p.source]);
  const after = run(process.execPath, [path.join(p.certifiedOut, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.equal(after.status, 0, after.stderr);
  assert.equal(after.stdout, before.stdout, name);
  const checked = checkTranslation(frontBundle(p.source), p.certifiedOut);
  assert.equal(checked.status, 0, describe(name, checked));
}

function byBehavior(name, code, side) {
  const p = prepareSideSplit(name, code, side);
  const unforced = lowerDiagnostic(p);
  if (unforced.certification.status === "refused") {
    certifiedBehaviorRefusals.push(name);
    assert(unforced.certification.reason, unforced.certification.log);
    assert.deepEqual(unforced.certification.files, []);
  } else {
    assert.equal(unforced.certification.status, "certified", unforced.certification.log);
    const before = run(process.execPath, [p.source]);
    const after = run(process.execPath, [path.join(p.certifiedOut, "entry.mjs")]);
    assert.equal(before.status, 0, before.stderr);
    assert.equal(after.status, 0, after.stderr);
    assert.equal(after.stdout, before.stdout, name);
    const checked = checkTranslation(frontBundle(p.source), p.certifiedOut);
    assert.equal(checked.status, 0, describe(name, checked));
  }
  if (unforced.status !== 0) assert.match(unforced.stdout, /FAIL\s+OrderOk/, name);
}

for (const [name, code, side] of [
  [
    "destructuring_after_descriptor_change",
    `var record = {first: 3, second: 4};
var alias = record;
Object.defineProperty(alias, "second", {get() { return factory(); }});
const factory = () => 7;
var { first, second } = record;
console.log(first, second);`,
    [0, 1, 2, 4],
  ],
  ["negated_record_read_tdz", `const flag = {};\nconst record = {value: !flag};\nconsole.log(record.value);`, [1]],
  ["literal_read_tdz", `const result = 7;\nconst alias = result;\nconsole.log(alias);`, [1, 2]],
  [
    "literal_callback",
    `function f() { return result; }\nglobalThis.saved = f;\nconst result = 7;\nconsole.log(globalThis.saved());`,
    [0, 1, 3],
  ],
  [
    "direct",
    `function f() { return result(); }\nglobalThis.saved = f;\nconst result = () => 7;\nconsole.log(globalThis.saved());`,
    [0, 1, 3],
  ],
  [
    "alias_chain",
    `function f() { return alias2(); }\nglobalThis.saved = f;\nconst result = () => 7;\nconst alias = result;\nconst alias2 = alias;\nconsole.log(globalThis.saved());`,
    [0, 1, 5],
  ],
  [
    "object_alias",
    `function f() { return alias.value(); }\nglobalThis.saved = f;\nconst result = () => 7;\nconst alias = {value: result};\nconsole.log(globalThis.saved());`,
    [0, 1, 4],
  ],
  [
    "array_alias",
    `function f() { return alias[0](); }\nglobalThis.saved = f;\nconst result = () => 7;\nconst alias = [result];\nconsole.log(globalThis.saved());`,
    [0, 1, 4],
  ],
  [
    "mutable_slot",
    `let slot;
function register(f) { slot = f; }
function trigger() { return slot(); }
function later() { return result(); }
register(later);
const result = () => 7;
console.log(trigger());`,
    [0, 1, 2, 3, 4, 6],
  ],
  ["generated_2026_3", fixture("seed2026_round3.js.txt"), [0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 12]],
  ["generated_2026_8", fixture("seed2026_round8.js.txt"), [0, 1, 2, 3, 4, 5, 6, 7, 10, 11]],
  // an escaped callback reads `x`, and the split runs the array's call before `x` is initialized
  [
    "array_reentry_call",
    `globalThis.saved = () => x;\nconst x = 1;\nconst arr = [globalThis.saved()];\nconsole.log(arr[0]);`,
    [0, 2],
  ],
  [
    "array_reentry_spread",
    `globalThis.saved = () => x;\nconst x = 1;\nconst arr = [...{ *[Symbol.iterator]() { yield globalThis.saved(); } }];\nconsole.log(arr[0]);`,
    [0, 2],
  ],
]) {
  refuse(name, code, side);
}

// An escaped array can have an own index getter, even with the original Array.prototype.includes.
// Both programs complete. Moving just the factory initialization changes what the callback observes.
{
  const name = "escaped_array_includes_observes_factory";
  const p = prepareSideSplit(
    name,
    `var ratios = [1, 2, 3];
globalThis.installObserver(ratios, () => typeof factory);
var scale = ratios.includes(2) ? 2 : 1;
var factory = () => 7;
console.log(scale);
export { ratios, factory };`,
    [0, 1, 3]
  );
  const host = path.join(temp, "array-observer.mjs");
  writeFileSync(
    host,
    `globalThis.installObserver = (ratios, observe) => {
  Object.defineProperty(ratios, "0", { get() { console.log(observe()); return 1; } });
};`
  );
  const rejected = lowerDiagnostic(p);
  assert.equal(rejected.status, 1, describe(name, rejected));
  assert.match(rejected.stdout, /FAIL\s+OrderOk/, name);
  assert.equal(rejected.certification.status, "refused", rejected.certification.log);
  assert(rejected.certification.reason, rejected.certification.log);
  assert.deepEqual(rejected.certification.files, []);
  const original = run(process.execPath, ["--import", host, p.source]);
  assert.equal(original.status, 0, describe(name, original));
  assert.equal(original.stdout, "undefined\n2\n", name);
}

// Exported declarations have no export-specifier read in the input AST, yet an external cyclic importer may observe
// their bindings during evaluation.
{
  const name = "export_declaration";
  const p = prepareSideSplit(name, `export const local = 7;\nglobalThis.hook();`, [1]);
  const rejected = lowerDiagnostic(p);
  assert.equal(rejected.status, 1, describe(name, rejected));
  assert.match(rejected.stdout, /FAIL\s+OrderOk/);
  assert.equal(rejected.certification.status, "refused", rejected.certification.log);
  assert(rejected.certification.reason, rejected.certification.log);
  assert.deepEqual(rejected.certification.files, []);
}

// Node control for the external-module cycle: `lowersplit` rejects import syntax, so these source modules exercise the
// ESM behavior being guarded directly.
for (const [name, reordered] of [
  ["cycle_original", false],
  ["cycle_reordered", true],
]) {
  const dir = path.join(temp, name);
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    path.join(dir, "observer.mjs"),
    `import { local } from "./entry.mjs";\nglobalThis.hook = () => console.log(local);`
  );
  writeFileSync(path.join(dir, "side.mjs"), `globalThis.hook();`);
  writeFileSync(
    path.join(dir, "entry.mjs"),
    reordered
      ? `import "./observer.mjs";\nimport "./side.mjs";\nexport const local = 7;`
      : `import "./observer.mjs";\nexport const local = 7;\nglobalThis.hook();`
  );
  const result = run(process.execPath, [path.join(dir, "entry.mjs")]);
  if (reordered) {
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /ReferenceError/);
  } else {
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, "7\n");
  }
}

for (const [name, code, side] of [
  // a parameter that shadows a module binding is not a read of it
  [
    "shadow",
    `function local(result) { return result; }\nconst result = () => 7;\nconsole.log("side");\nconsole.log(result());`,
    [2],
  ],
  // the two declarations initialize in the other order; the callback runs after both
  ["constant_pair", `const a = 1;\nconst b = 2;\nfunction f() { return a + b; }\nconsole.log(f());`, [1]],
  [
    "literal_arrow",
    `const x = 1;\nconst f = () => 2;\nfunction see() { return [x, f]; }\nconsole.log(see()[0], see()[1]());`,
    [1],
  ],
  [
    "literal_record",
    `const x = 1;\nconst o = { color: "v", enabled: true };\nfunction see() { return [x, o]; }\nconsole.log(see()[0], see()[1].color);`,
    [1],
  ],
  [
    "literal_array",
    `const x = 1;\nconst a = ["v",, true];\nfunction see() { return [x, a]; }\nconsole.log(see()[0], see()[1][0], 1 in see()[1]);`,
    [1],
  ],
  // The receiver is private and dense, and the search value is a primitive.
  [
    "private_array_includes_before_factory",
    `var ratios = [1, 2, 3];
var scale = ratios.includes(2) ? 2 : 1;
var factory = () => 7;
console.log(scale, factory());`,
    [0, 2],
  ],
  // a read of an earlier initialized binding may move across an unrelated effect
  [
    "prior_initialized_read",
    `const value = 1;\nconst alias = value;\nconsole.log("side");\nconsole.log(alias);`,
    [0, 2],
  ],
]) {
  accept(name, code, side);
}
assert.deepEqual(certifiedControlRefusals, ["literal_array", "private_array_includes_before_factory"]);
console.log("certified inert escape controls:", JSON.stringify({ attempted: 7, refused: certifiedControlRefusals }));
console.log("certified inert behavior controls:", JSON.stringify({ attempted: 3, refused: certifiedBehaviorRefusals }));

// Judged by what Node prints.
for (const [name, code, side] of [
  [
    "arrow_captures_literal",
    `const x = 1;\nconst f = () => x;\nfunction expose() { return f; }\nconsole.log(expose()());`,
    [1],
  ],
  [
    "two_observed_arrows",
    `const f = () => 1;\nconst g = () => 2;\nfunction see() { return [f, g]; }\nconsole.log(see()[0](), see()[1]());`,
    [1],
  ],
  [
    "two_observed_arrays",
    `const a = [1];\nconst b = [2];\nfunction see() { return [a, b]; }\nconsole.log(see());`,
    [1],
  ],
]) {
  byBehavior(name, code, side);
}

// Read the global separately so that no array operation or coercion can mask getter reentry.
{
  const name = "host_global_getter_observes_factory";
  const p = prepareSideSplit(
    name,
    `var values = [1, 2, 3];
globalThis.observe = () => typeof factory;
var ratio = devicePixelRatio;
var factory = () => 7;
console.log(values.includes(ratio));`,
    [0, 1, 3]
  );
  const host = path.join(temp, "host-global-getter.mjs");
  writeFileSync(
    host,
    `Object.defineProperty(globalThis, "devicePixelRatio", {
    get() { console.log(globalThis.observe()); return 2; }
  });`
  );
  const verdict = lowerDiagnostic(p);
  assert.equal(verdict.certification.status, "refused", verdict.certification.log);
  assert(verdict.certification.reason, verdict.certification.log);
  assert.deepEqual(verdict.certification.files, []);
  const original = run(process.execPath, ["--import", host, p.source]);
  assert.equal(original.status, 0, describe(name, original));
  assert.equal(original.stdout, "undefined\ntrue\n", name);
  assert.match(verdict.stdout, /FAIL\s+OrderOk/, name);
}

// Both constructors finish normally. Only the position of the factory initializer changes.
for (const [name, setup, length] of [
  ["replaced_array_constructor", "globalThis.Array = function () { console.log(typeof factory); };", "3"],
  ["array_length_coercion", "var width = {valueOf() { console.log(typeof factory); return 3; }};", "(width + 2) * 2"],
]) {
  const p = prepareSideSplit(name, `${setup}\nvar slots = new Array(${length});\nvar factory = () => 7;`, [2]);
  const verdict = lowerDiagnostic(p);
  assert.equal(verdict.certification.status, "refused", verdict.certification.log);
  assert(verdict.certification.reason, verdict.certification.log);
  assert.deepEqual(verdict.certification.files, []);
  const original = run(process.execPath, [p.source]);
  assert.equal(original.status, 0, describe(name, original));
  assert.equal(original.stdout, "undefined\n", name);
  assert.equal(verdict.status, 1, describe(name, verdict));
  assert.match(verdict.stdout, /FAIL\s+OrderOk/, name);
}
