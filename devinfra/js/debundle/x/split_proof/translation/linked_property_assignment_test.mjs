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
  property,
  returns,
  root,
  throws,
  variable,
} from "./linked_programs_test_support.mjs";

const entry = "entry.mjs";
const worker = "worker.mjs";

function split(name, source, modules = { [worker]: ["#0"], [entry]: ["#1", "#2"] }, order = [worker]) {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider: "", spec: { entry, modules, order, sequential: true } });
  const out = path.join(dir, "out");
  return { dir, files, out, ...lower(files.sourceFile, files.specFile, out) };
}

function assertCertified({ files, out, verdict, log }, expected) {
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: expected }, log);
  }
}

const assignment = (left, right) => ({ type: "AssignmentExpression", operator: "=", left, right });
const binary = (operator, left, right) => ({ type: "BinaryExpression", operator, left, right });
const member = (object, property, computed = false) => ({ type: "MemberExpression", object, property, computed });
const sequence = (...expressions) => ({ type: "SequenceExpression", expressions });
const record = (...properties) => ({ type: "ObjectExpression", properties });
const declare = (name, init) => variable("const", name, init);
const trace = member(id("trace"), id("step"));
const mark = (previous, next) =>
  conditional(
    binary("===", trace, literal(previous)),
    assignment(trace, literal(next)),
    call(arrow([], block([throws(literal("unexpected operand evaluation"))])))
  );
const guard = (actual, expected) => ({
  type: "IfStatement",
  test: binary("!==", actual, literal(expected)),
  consequent: throws(literal("unexpected assignment behavior")),
  alternate: null,
});

// Guard the model itself against evaluating an operand out of order or twice.
for (const [name, key, computed, value] of [
  ["dot", "value", false, "after"],
  ["computed-string", "value", true, false],
  ["computed-number", 0, true, 42],
]) {
  const keySlot = member(id("keys"), id("slot"));
  const computedKey = sequence(mark("base", "key"), assignment(keySlot, literal(key)));
  const field = computed ? literal(key) : id(key);
  const read = member(id("target"), field, computed);
  const write = member(sequence(mark("start", "base"), id("target")), computed ? computedKey : field, computed);
  const run = arrow(
    [],
    block([
      declare("target", record({ ...property("value", literal("before")), key: literal(String(key)) })),
      declare("keys", record(property("slot", literal("before")))),
      declare("trace", record(property("step", literal("start")))),
      declare("assigned", assignment(write, sequence(mark(computed ? "key" : "base", "value"), literal(value)))),
      guard(trace, "value"),
      guard(id("assigned"), value),
      guard(read, value),
      guard(keySlot, computed ? key : "before"),
      returns(id("assigned")),
    ])
  );
  const source = program([
    declare("run", run),
    declare("answer", call(id("run"))),
    exportNamed([["answer", "result"]]),
  ]);
  assertCertified(split(name, source), value);
}

// Abrupt base and key evaluation skips the remaining operands. A throwing RHS
// leaves the property unchanged. The nullish write fails after the RHS runs.
assertCertified(
  split(
    "abrupt-assignment",
    `const run = (phase, expectedStep) => {
      const target = { value: "before" };
      const trace = { step: "start" };
      const step = (previous, next, value) => {
        if (trace.step !== previous) throw "unexpected operand evaluation";
        trace.step = next;
        if (phase === next) throw next;
        return value;
      };
      try {
        step("start", "base", phase === "null" ? null : target)[step("base", "key", "value")] = step("key", "rhs", "after");
      } catch (error) {
        const matches = phase === "null" ? typeof error === "object" : error === phase;
        if (matches && trace.step === expectedStep && target.value === "before") return trace.step;
        throw error;
      }
      throw "assignment must throw";
    };
    const answer = [run("base", "base"), run("key", "key"), run("rhs", "rhs"), run("null", "rhs")];
    export { answer as result };`
  ),
  ["base", "key", "rhs", "rhs"]
);

{
  const source = `const state = { value: "before" };
    state.value = "first";
    const seen = state.value;
    state["value"] = "second";
    export const result = { seen, final: state.value };`;
  const state = "state.mjs";
  const first = "first.mjs";
  const read = "read.mjs";
  const second = "second.mjs";
  const modules = { [state]: ["#0"], [first]: ["#1"], [read]: ["#2"], [second]: ["#3"], [entry]: ["#4"] };
  const original = { seen: "first", final: "second" };
  assertCertified(split("ordered-writes", source, modules, [state, first, read, second]), original);

  // The final write and the intermediate read each constrain the schedule.
  for (const [name, order, expected] of [
    ["reordered-writes", [state, second, read, first], { seen: "second", final: "first" }],
    ["early-read", [state, read, first, second], { seen: "before", final: "second" }],
  ]) {
    const { dir, files, out, verdict, log } = split(name, source, modules, order);
    assert.equal(verdict.status, "rejected", log);
    assert.match(verdict.reason, /probe executions completed normally with different final states/, log);
    assert(!existsSync(out));
    const preview = path.join(dir, "preview");
    forcePreview(files, preview);
    assert.deepEqual(moduleEvaluationOrder(preview, entry), [...order, entry]);
    assert.deepEqual(observe(files.sourceFile, "result"), { result: original });
    assert.deepEqual(observe(path.join(preview, entry), "result"), { result: expected });
  }
}
