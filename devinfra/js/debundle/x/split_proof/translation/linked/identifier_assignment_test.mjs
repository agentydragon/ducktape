import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  assertCertified,
  arrow,
  assertCertifiedResult,
  assertRejected,
  block,
  call,
  caseShard,
  exportNamed,
  forcePreview,
  functionExpression,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  program,
  returns,
  sequentialSplit,
  throws,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const worker = "worker.mjs";
const includesCase = caseShard();
const load = (file) => import(pathToFileURL(file).href);

function requireCertified({ verdict, log }) {
  assertCertified(verdict, log);
}

// The setter is deferred until after entry initialization. Its write must still
// reach the same cell as the getter and the entry's live export.
if (includesCase("cell-owner")) {
  const source = `let current;
    function write(value) { return current = value; }
    function read() { return current; }
    const initial = read();
    export { current, write, read, initial };`;
  const valid = sequentialSplit("cell-owner", source, { [worker]: ["#0", "#1", "#2"], [entry]: ["#3", "#4"] });
  requireCertified(valid);
  for (const file of [valid.files.sourceFile, path.join(valid.out, entry)]) {
    const service = await load(file);
    assert.equal(service.initial, undefined);
    assert.equal(service.current, undefined);
    assert.equal(service.write("updated"), "updated");
    assert.equal(service.current, "updated");
    assert.equal(service.read(), "updated");
  }

  const writer = "writer.mjs";
  const modules = { [worker]: ["#0", "#2"], [writer]: ["#1"], [entry]: ["#3", "#4"] };
  const invalid = sequentialSplit("moved-writer", source, modules, [worker, writer]);
  assertRejected(invalid, /source placement or module linkage checks failed/);
  const preview = path.join(invalid.dir, "preview");
  forcePreview(invalid.files, preview);
  assert.deepEqual(moduleEvaluationOrder(preview, entry), [worker, writer, entry]);
  const original = await load(invalid.files.sourceFile);
  assert.equal(original.write("updated"), "updated");
  assert.equal(original.current, "updated");
  const service = await load(path.join(preview, entry));
  assert.equal(service.initial, undefined);
  assert.throws(() => service.write("updated"), TypeError);
  assert.equal(service.read(), undefined);
  assert.equal(service.current, undefined);
}

// The import aliases a mutable provider cell. Both trees must evaluate the RHS
// once, catch the failed write, and retain the provider's value.
if (includesCase("imported-write")) {
  const source = `import { value as shared } from "./provider.mjs";
    let evaluations = 0;
    function write() { return shared = (evaluations = evaluations + 1, "wrong"); }
    function shadow(shared) {
      const parameter = (shared = "parameter");
      {
        let shared = "before";
        return [parameter, shared = "block"];
      }
    }
    const run = () => {
      try {
        write();
        throw "assignment unexpectedly succeeded";
      } catch (error) {
        if (typeof error !== "object" || evaluations !== 1) throw error;
      }
      return [shared, evaluations, shadow("argument")];
    };
    const result = run();
    export { result, write, shared };`;
  const test = sequentialSplit(
    "imported-write",
    source,
    { [entry]: ["#0", "#4", "#5", "#6"], [worker]: ["#1", "#2", "#3"] },
    [worker],
    'export let value = "original";'
  );
  assertCertifiedResult(test, ["original", 1, ["parameter", "block"]]);
  requireCertified(lower(test.files.sourceFile, test.files.specFile, test.out, ["--check-output"]));
  for (const file of [test.files.sourceFile, path.join(test.out, entry)]) {
    const service = await load(file);
    assert.throws(() => service.write(), TypeError);
    assert.equal(service.shared, "original");
  }
}

// Check the model's abrupt handling as well as the Node results. The attempted
// writes to an uninitialized binding and a constant run their RHS first.
if (includesCase("abrupt-and-once")) {
  assertCertifiedResult(
    sequentialSplit(
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
}

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
  if (!includesCase(name)) continue;
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
  assertCertifiedResult(sequentialSplit(name, source), expected);
}

// A var binding already exists when the reader runs, so both orders complete.
// Placing the read before the assignment changes the observed value.
if (includesCase("assignment-order")) {
  const source = `var current; current = "ready"; const result = typeof current; export { result };`;
  const reader = "reader.mjs";
  const modules = { [worker]: ["#0", "#1"], [reader]: ["#2"], [entry]: ["#3"] };
  assertCertifiedResult(sequentialSplit("ordered", source, modules, [worker, reader]), "string");
  const test = sequentialSplit("early-reader", source, modules, [reader, worker]);
  assertRejected(test, /probe executions completed normally with different final states/);
  const preview = path.join(test.dir, "preview");
  forcePreview(test.files, preview);
  assert.deepEqual(moduleEvaluationOrder(preview, entry), [reader, worker, entry]);
  assert.deepEqual(observe(test.files.sourceFile, "result"), { result: "string" });
  assert.deepEqual(observe(path.join(preview, entry), "result"), { result: "undefined" });
}
