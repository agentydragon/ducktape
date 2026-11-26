import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { parse } from "acorn";
import path from "node:path";
import {
  assertCertified,
  assertRejectedOrOrderKeptSlice,
  arrow,
  assertCertifiedResult,
  binary,
  block,
  call,
  caseShard,
  conditional,
  exportNamed,
  forcePreview,
  id,
  literal,
  lower,
  member,
  moduleEvaluationOrder,
  observe,
  program,
  property,
  returns,
  sequentialSplit,
  throws,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const worker = "worker.mjs";
const includesCase = caseShard();

const assignment = (left, right) => ({ type: "AssignmentExpression", operator: "=", left, right });
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

// Relocate a complete numeric enum initializer. Duplicate numeric values keep
// both forward names and replace the reverse name on the second assignment.
for (const [scenario, values, initialFields] of [
  ["duplicate", [3, 0, 3], {}],
  ["fresh", Array.from({ length: 89 }, (_, i) => (i * 7) % 89), {}],
  ["existing", Array.from({ length: 32 }, (_, i) => i), { Member3: "old", keep: "original" }],
]) {
  const name = `numeric-enum-${scenario}`;
  if (!includesCase(name)) continue;
  const object = id("table");
  const expected = { ...initialFields };
  const writes = values.map((value, i) => {
    const name = `Member${i}`;
    expected[name] = value;
    expected[value] = name;
    const forward = assignment(member(object, i % 2 ? literal(name) : id(name), Boolean(i % 2)), literal(value));
    return assignment(member(object, forward, true), literal(name));
  });
  const old = record(...Object.entries(initialFields).map(([k, v]) => property(k, literal(v))));
  const initial = { type: "LogicalExpression", operator: "||", left: id("codes"), right: old };
  const initializer = call(arrow([object], sequence(...writes, object)), [initial]);
  const source = program([variable("var", "codes", initializer), exportNamed([["codes", "Codes"]])]);
  const { files, out, verdict, log } = sequentialSplit(name, source, {
    [worker]: ["#0"],
    [entry]: ["#1"],
  });
  assertCertified(verdict, log);
  assert.deepEqual(moduleEvaluationOrder(out, entry), [worker, entry]);
  const ownerAST = parse(readFileSync(path.join(out, worker), "utf8"), { ecmaVersion: "latest", sourceType: "module" });
  assert(
    ownerAST.body.some((stmt) =>
      (stmt.declaration ?? stmt).declarations?.some((d) => d.id.name === "codes" && d.init?.type === "CallExpression")
    )
  );

  const disk = lower(files.sourceFile, files.specFile, out, ["--check-output"]);
  assert.equal(disk.verdict.status, "certified", disk.log);
  assert.equal(
    disk.verdict.guarantee,
    disk.verdict.scope === "linked-slice-order-kept" ? "all-outcomes" : "normal-completion",
    disk.log
  );
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    const namespace = await import(pathToFileURL(file).href);
    assert.deepEqual(Object.keys(namespace), ["Codes"]);
    assert.deepEqual(Reflect.ownKeys(namespace.Codes), Reflect.ownKeys(expected));
    assert.deepEqual(Object.getOwnPropertyDescriptors(namespace.Codes), Object.getOwnPropertyDescriptors(expected));
  }
}

// The receiver's non-writable length stops the first assignment before any later writes.
if (includesCase("readonly-batch")) {
  const target = id("table");
  const keys = ["length", ...Array.from({ length: 63 }, (_, i) => `field${i}`)];
  const writes = keys.map((key, i) => assignment(member(target, literal(key), true), literal(i)));
  const init = call(arrow([target], sequence(...writes, target)), [arrow([], literal(0))]);
  const source = program([declare("codes", init), exportNamed([["codes", "Codes"]])]);
  const { files, out, verdict, log } = sequentialSplit("readonly-batch", source, { [worker]: ["#0"], [entry]: ["#1"] });
  const orderKept = assertRejectedOrOrderKeptSlice(
    { verdict, log, out },
    /original threw during linked initialization/
  );
  assert.deepEqual(observe(files.sourceFile, "Codes"), { error: "TypeError" });
  if (orderKept) assert.deepEqual(observe(path.join(out, entry), "Codes"), { error: "TypeError" });
}

// Guard the model itself against evaluating an operand out of order or twice.
for (const [name, key, computed, value] of [
  ["dot", "value", false, "after"],
  ["computed-string", "value", true, false],
  ["computed-number", 0, true, 42],
]) {
  if (!includesCase(name)) continue;
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
  assertCertifiedResult(sequentialSplit(name, source), value);
}

// Abrupt base and key evaluation skips the remaining operands. A throwing RHS
// leaves the property unchanged. The nullish write fails after the RHS runs.
if (includesCase("abrupt-assignment")) {
  assertCertifiedResult(
    sequentialSplit(
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
}

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
  if (includesCase("ordered-writes")) {
    assertCertifiedResult(sequentialSplit("ordered-writes", source, modules, [state, first, read, second]), original);
  }

  // The final write and the intermediate read each constrain the schedule.
  for (const [name, order, expected] of [
    ["reordered-writes", [state, second, read, first], { seen: "second", final: "first" }],
    ["early-read", [state, read, first, second], { seen: "before", final: "second" }],
  ]) {
    if (!includesCase(name)) continue;
    const { dir, files, out, verdict, log } = sequentialSplit(name, source, modules, order);
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
