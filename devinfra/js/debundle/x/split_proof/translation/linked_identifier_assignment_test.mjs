import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  arrow,
  block,
  call,
  exportNamed,
  forcePreview,
  functionExpression,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  prepare,
  program,
  returns,
  root,
  throws,
  variable,
} from "./linked_programs_test_support.mjs";

const entry = "entry.mjs";
const worker = "worker.mjs";
const load = (file) => import(pathToFileURL(file).href);

function split(name, source, modules = { [worker]: ["#0"], [entry]: ["#1", "#2"] }, order = [worker], provider = "") {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider, spec: { entry, modules, order, sequential: true } });
  const out = path.join(dir, "out");
  return { files, out, dir, ...lower(files.sourceFile, files.specFile, out) };
}

function requireCertified({ verdict, log }) {
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", log);
}

function requireRejected({ verdict, log, out }, reason) {
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, reason, log);
  assert(!existsSync(out));
}

function assertResult(test, expected) {
  requireCertified(test);
  for (const file of [test.files.sourceFile, path.join(test.out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: expected }, test.log);
  }
}

// The setter is deferred until after entry initialization. Its write must still
// reach the same cell as the getter and the entry's live export.
{
  const source = `let current;
    function write(value) { return current = value; }
    function read() { return current; }
    const initial = read();
    export { current, write, read, initial };`;
  const valid = split("cell-owner", source, { [worker]: ["#0", "#1", "#2"], [entry]: ["#3", "#4"] });
  requireCertified(valid);
  for (const file of [valid.files.sourceFile, path.join(valid.out, entry)]) {
    const service = await load(file);
    assert.equal(service.initial, undefined);
    assert.equal(service.current, undefined);
    assert.equal(service.write("updated"), "updated");
    assert.equal(service.current, "updated");
    assert.equal(service.read(), "updated");
  }

  const modules = { [worker]: ["#0", "#2"], "writer.mjs": ["#1"], [entry]: ["#3", "#4"] };
  const invalid = split("moved-writer", source, modules, [worker, "writer.mjs"]);
  requireRejected(invalid, /source placement or module linkage checks failed/);
  const preview = path.join(invalid.dir, "preview");
  forcePreview(invalid.files, preview);
  const service = await load(path.join(preview, entry));
  assert.equal(service.initial, undefined);
  assert.throws(() => service.write("updated"), TypeError);
  assert.equal(service.read(), undefined);
  assert.equal(service.current, undefined);
}

// A caught error can hide an imported write at initialization. Reject the write
// in the syntax instead of treating the import as a mutable alias of its cell.
{
  const source = `import { shared } from "./provider.mjs";
    const run = () => {
      try { return shared = "wrong"; } catch { return shared; }
    };
    export const result = run();`;
  const test = split(
    "imported-write",
    source,
    { [entry]: ["#0", "#2"], [worker]: ["#1"] },
    [worker],
    'export let shared = "original";'
  );
  assert.deepEqual(observe(test.files.sourceFile, "result"), { result: "original" });
  requireRejected(test, /unsupported linked assignment to imported binding shared/);
}

// Check the model's abrupt handling as well as the Node results. The attempted
// writes to an uninitialized binding and a constant run their RHS first.
assertResult(
  split(
    "abrupt-and-once",
    `const run = (input) => {
    let current = "fallback";
    let step = "start";
    const read = () => {
      if (step !== "start") throw "read twice";
      step = "read";
      return input.property.name;
    };
    try {
      if ((current = read()) !== current) throw "incorrect assignment value";
    } catch (error) {
      if (typeof error !== "object") throw error;
    }
    if (step !== "read") throw "expected RHS evaluation";
    try { later = (step = "tdz"); throw "expected a TDZ error"; }
    catch (error) { if (typeof error !== "object" || step !== "tdz") throw error; }
    let later = "ready";
    const fixed = true;
    try { fixed = (step = "const"); throw "expected a const error"; }
    catch (error) { if (typeof error !== "object" || step !== "const") throw error; }
    return [current, later, fixed, step];
  };
  const result = [run(null), run({ property: { name: "value" } })];
  export { result };`
  ),
  [
    ["fallback", "ready", true, "const"],
    ["value", "ready", true, "const"],
  ]
);

const fn = functionExpression([], block([returns(literal(42))]));
const anonArrow = arrow([], literal(42));
const readMember = (object, property) => ({ type: "MemberExpression", object, property, computed: false });
const checkEqual = (left, right) => ({
  type: "IfStatement",
  test: { type: "BinaryExpression", operator: "!==", left, right },
  consequent: throws(literal("incorrect assignment")),
  alternate: null,
});

for (const [name, rhs, expected] of [
  ["anonymous-arrow", anonArrow, "callback"],
  ["anonymous-function", fn, "callback"],
  ["named-function", { ...fn, id: id("original") }, "original"],
  ["sequence-rhs", { type: "SequenceExpression", expressions: [literal(0), anonArrow] }, ""],
]) {
  const assign = { type: "AssignmentExpression", operator: "=", left: id("callback"), right: rhs };
  const observedName = readMember(id("assigned"), id("name"));
  const run = arrow(
    [],
    block([
      variable("let", "callback", null),
      variable("const", "assigned", assign),
      checkEqual(id("assigned"), id("callback")),
      checkEqual(observedName, literal(expected)),
      checkEqual(call(id("assigned")), literal(42)),
      returns(observedName),
    ])
  );
  const source = program([
    variable("const", "run", run),
    variable("const", "result", call(id("run"))),
    exportNamed([["result", "result"]]),
  ]);
  assertResult(split(name, source), expected);
}

// A var binding already exists when the reader runs, so both orders complete.
// Placing the read before the assignment changes the observed value.
{
  const source = `var current; current = "ready"; const result = typeof current; export { result };`;
  const reader = "reader.mjs";
  const modules = { [worker]: ["#0", "#1"], [reader]: ["#2"], [entry]: ["#3"] };
  assertResult(split("ordered", source, modules, [worker, reader]), "string");
  const test = split("early-reader", source, modules, [reader, worker]);
  requireRejected(test, /probe executions completed normally with different final states/);
  const preview = path.join(test.dir, "preview");
  forcePreview(test.files, preview);
  assert.deepEqual(moduleEvaluationOrder(preview, entry), [reader, worker, entry]);
  assert.deepEqual(observe(test.files.sourceFile, "result"), { result: "string" });
  assert.deepEqual(observe(path.join(preview, entry), "result"), { result: "undefined" });
}
