import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  arrow,
  block,
  call,
  conditional,
  exportNamed,
  forcePreview,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  prepare,
  program,
  root,
  throws,
  variable,
} from "./linked_programs_test_support.mjs";

const entry = "entry.mjs";

function split(name, source, modules) {
  const dir = path.join(root, name);
  const spec = { entry, modules, order: ["worker.mjs"], sequential: true };
  const files = prepare(dir, { source, spec, provider: "" });
  const out = path.join(dir, "out");
  return { dir, files, out, ...lower(files.sourceFile, files.specFile, out) };
}

function assertWorkerResult(name, source, expected) {
  const { files, out, verdict, log } = split(name, source, { "worker.mjs": ["#0"], [entry]: ["#1", "#2"] });
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: expected }, name);
  }
}

// Generate different operand counts and both direct and deferred sequences.
// The guard makes a wrong final value fail in the model even if both sides lower it the same way.
for (const [index, operands] of [
  [true, 0],
  [null, "ignored", false],
  [1, 2, 3, "last"],
  [...Array.from({ length: 89 }, (_, i) => i), "wide final value"],
].entries()) {
  const expected = operands.at(-1);
  const sequence = { type: "SequenceExpression", expressions: operands.map(literal) };
  const defer = index % 2 === 0;
  const test = {
    type: "BinaryExpression",
    operator: "===",
    left: defer ? call(id("worker")) : id("worker"),
    right: literal(expected),
  };
  const reject = call(arrow([], block([throws(literal("wrong sequence value"))])));
  const source = program([
    variable("const", "worker", defer ? arrow([], sequence) : sequence),
    variable("const", "value", conditional(test, literal(expected), reject)),
    exportNamed([["value", "result"]]),
  ]);
  assertWorkerResult(`generated-sequence-${index}`, source, expected);
}

assertWorkerResult(
  "first-abrupt-operand",
  `const worker = () => {
    const stop = (message) => { throw message; };
    try { return (0, stop("first"), stop("last")); }
    catch (error) {
      if (error === "first") return error;
      throw error;
    }
  };
  const value = worker();
  export { value as result };`,
  "first"
);

// The member access provides a receiver only for the direct call.
assertWorkerResult(
  "detached-receiver",
  `const worker = () => {
    const read = function () { return typeof this; };
    const instance = { read };
    const direct = instance.read();
    const detached = (0, instance.read)();
    if (direct === "object" && detached === "undefined") return detached;
    throw "wrong sequence receiver";
  };
  const value = worker();
  export { value as result };`,
  "undefined"
);

// The sequence operand does not inherit the static property key as its function name.
assertWorkerResult(
  "sequence-property-value",
  `const worker = { run: (0, () => 9) };
  const value = worker.run.name === "" ? worker.run() : (() => { throw "wrong function name"; })();
  export { value as result };`,
  9
);

// The discarded read can still throw before the following operand. Moving the
// late declaration earlier changes which error reaches the catch.
for (const dependent of [false, true]) {
  const source = `const stop = () => { throw "stopped"; };
    const read = () => {
      try { return (${dependent ? "late" : "0"}, stop(), 1); }
      catch (error) { return typeof error; }
    };
    const value = read();
    const late = 2;
    export { value as result };`;
  const { dir, files, out, verdict, log } = split(`reordered-sequence-${dependent}`, source, {
    "worker.mjs": ["#3"],
    [entry]: ["#0", "#1", "#2", "#4"],
  });
  assert.deepEqual(observe(files.sourceFile, "result"), { result: dependent ? "object" : "string" });
  let candidate = out;
  if (dependent) {
    assert.equal(verdict.status, "rejected", log);
    assert.match(verdict.reason, /probe executions completed normally with different final states/);
    assert(!existsSync(out));
    candidate = path.join(dir, "preview");
    forcePreview(files, candidate);
  } else {
    assert.equal(verdict.status, "certified", log);
    assert.equal(verdict.guarantee, "normal-completion", log);
  }
  assert.deepEqual(moduleEvaluationOrder(candidate, entry), ["worker.mjs", entry]);
  assert.deepEqual(observe(path.join(candidate, entry), "result"), { result: "string" });
}
