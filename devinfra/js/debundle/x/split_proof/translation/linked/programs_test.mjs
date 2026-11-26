import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  id,
  literal,
  call,
  caseShard,
  conditional,
  returns,
  throws,
  block,
  arrow,
  workerSplit,
  assertWorkerResult,
  assertRejectedOrOrderKeptSlice,
  observe,
  observedProgram,
} from "./harness.mjs";

const includesCase = caseShard();

function checkWorkerResult(name, source, expected) {
  if (includesCase(name)) assertWorkerResult(name, source, expected);
}

for (const [seed, value] of [0, "taken", "", 3].entries()) {
  const selected = literal(seed + 30);
  const skipped = call(arrow([], block([throws(literal(seed))])));
  const choose = conditional(literal(value), value ? selected : skipped, value ? skipped : selected);
  const source = observedProgram(arrow([], choose), call(id("worker")));
  checkWorkerResult(`generated-conditional-${seed}`, source, seed + 30);
}

// The temporal dead zone throws from the test and from the selected arm respectively.
// Returning normally through either expression would bypass the catch.
for (const [name, expr] of [
  ["test", "later ? 1 : 2"],
  ["arm", "true ? later : 2"],
]) {
  const source = `const worker = () => {
    try { return ${expr}; let later; } catch { return 7; }
  };
  export const result = worker();`;
  checkWorkerResult(`conditional-abrupt-${name}`, source, 7);
}

// The wrong comparison result throws during initialization, so an incorrect lowering
// cannot pass by making the same mistake in both the original and the split.
for (const [index, [left, op, right, expected]] of [
  [literal("1"), "===", literal(1), false],
  [id("worker"), "===", id("worker"), true],
  [id("worker"), "!==", { type: "ObjectExpression", properties: [] }, true],
  [id("NaN"), "===", id("NaN"), false],
].entries()) {
  const test = { type: "BinaryExpression", operator: op, left, right };
  const abrupt = call(arrow([], block([throws(literal(index))])));
  const result = conditional(test, expected ? literal(true) : abrupt, expected ? abrupt : literal(false));
  checkWorkerResult(
    `strict-comparison-${index}`,
    observedProgram({ type: "ObjectExpression", properties: [] }, result),
    expected
  );
}

// A void operand still runs. Failing any assertion during initialization
// prevents an identical lowering mistake in both modules from certifying.
checkWorkerResult(
  "void-observes-operand",
  `const worker = () => {
    const box = {value: 0};
    const discarded = void (box.value = 4);
    if (discarded !== undefined || box.value !== 4) throw "operand";
    try { return void (() => { throw "abrupt"; })(); }
    catch (error) { return error; }
  };
  export const result = worker();`,
  "abrupt"
);

{
  const voidExpr = (argument) => ({ type: "UnaryExpression", operator: "void", argument, prefix: true });
  const operands = [literal(0), literal(null), arrow([], literal(3)), call(arrow([], literal("returned")))];
  const checks = operands.map((operand) => ({
    type: "IfStatement",
    test: { type: "BinaryExpression", operator: "!==", left: voidExpr(operand), right: id("undefined") },
    consequent: throws(literal("unexpected value")),
    alternate: null,
  }));
  checkWorkerResult(
    "generated-void-operands",
    observedProgram(arrow([], block([...checks, returns(literal(true))])), call(id("worker"))),
    true
  );
}

const typeOf = (argument) => ({ type: "UnaryExpression", operator: "typeof", prefix: true, argument });

// Exercise both direct evaluation and calls during initialization. The first seed
// reproduces the proof-cost regression on a moved worker returning "undefined" != typeof undefined.
for (const [index, [operand, tag, expected]] of [
  [id("undefined"), "undefined", false],
  [arrow([], literal(0)), "object", true],
  [literal(null), "object", false],
  [literal(3), "string", true],
].entries()) {
  const type = typeOf(operand);
  const [left, right] = index % 2 === 0 ? [literal(tag), type] : [type, literal(tag)];
  const expr = { type: "BinaryExpression", operator: "!=", left, right };
  const source = index < 2 ? observedProgram(arrow([], expr), call(id("worker"))) : observedProgram(expr, id("worker"));
  checkWorkerResult(`generated-string-inequality-${index}`, source, expected);
}

// If the model gets a branch wrong, the initializer throws before certification.
for (const [index, [left, right, equal]] of [
  [literal("01"), literal("1"), false],
  [literal("undefined"), typeOf(id("undefined")), true],
  [typeOf(literal(null)), typeOf(literal(3)), false],
].entries()) {
  const comparison = { type: "BinaryExpression", operator: "==", left, right };
  const wrong = call(arrow([], block([throws(literal("wrong equality result"))])));
  const expected = literal(equal);
  const result = conditional(comparison, equal ? expected : wrong, equal ? wrong : expected);
  const source = observedProgram(arrow([], result), call(id("worker")));
  checkWorkerResult(`generated-string-equality-${index}`, source, equal);
}

checkWorkerResult(
  "string-equality-operand-order",
  `const worker = () => {
    const trace = { step: "start" };
    const left = () => {
      if (trace.step !== "start") throw "left repeated";
      trace.step = "left";
    };
    const right = () => {
      if (trace.step !== "left") throw "right out of order";
      trace.step = "right";
      return 1;
    };
    if (typeof left() == typeof right()) throw "unexpected equality";
    const failing = () => { trace.step = "failed"; throw "left stopped"; };
    try { typeof failing() == typeof right(); }
    catch (error) {
      if (error !== "left stopped" || trace.step !== "failed") throw error;
      return "done";
    }
    throw "missing throw";
  };
  export const result = worker();`,
  "done"
);

// An unbounded call chain still exhausts the ordinary search budget.
if (includesCase("recursive-fuel")) {
  const { verdict, out } = workerSplit(
    "recursive-fuel",
    observedProgram(arrow([], call(id("worker"))), call(id("worker")))
  );
  const orderKept = assertRejectedOrOrderKeptSlice(
    { verdict, log: JSON.stringify(verdict), out },
    /exhausted the fuel search budget \(64\)/
  );
  if (!orderKept) assert.equal(verdict.reason_code, "probe_fuel");
}

checkWorkerResult(
  "hoisted-local-function",
  `const worker = (offset) => {
    const answer = read(2);
    if (answer !== 42) throw "incorrect hoisting or capture";
    return answer;

    function read(remaining) {
      return remaining === 0 ? offset : read(remaining - 1);
    }
  };
  export const result = worker(42);`,
  42
);

for (const [name, body, reason] of [
  ["parameter", "function read() {} return read();", /overlaps a parameter or var binding/],
  ["var", "var local; function local() {} return local();", /overlaps a parameter or var binding/],
  ["duplicate", "function local() {} function local() {} return local();", /duplicate local function declaration/],
]) {
  const caseName = `local-function-${name}`;
  if (!includesCase(caseName)) continue;
  const { verdict, out } = workerSplit(
    caseName,
    `const worker = (read) => { ${body} }; export const result = worker();`
  );
  assert.equal(verdict.status, "rejected");
  assert.match(verdict.reason, reason, name);
  assert(!existsSync(out));
}

// Different numeric spellings compare by string contents.
checkWorkerResult("unequal-numeric-strings", 'const different = "01" != "1"; export const result = different;', true);
checkWorkerResult(
  "typeof-local-tdz",
  'const worker = () => { try { return "undefined" != typeof later; let later; } catch { return 7; } }; export const result = worker();',
  7
);
checkWorkerResult(
  "typeof-shadowed-host-name",
  'const window = null; export const result = "undefined" != typeof window;',
  true
);
for (const [name, expr, expected] of [
  ["mixed-equality", '"01" == 1', true],
  ["mixed-inequality", '"01" != 1', false],
  ["object-identity", "worker == worker", true],
  ["nullish-equality", "undefined == null", true],
  ["nullish-object", "({ valueOf() { throw 1; } }) != null", true],
  ["boolean-conversion", 'true == "01"', true],
  ["NaN-equality", "NaN == NaN", false],
]) {
  const source = `const worker = () => { if ((${expr}) !== ${expected}) throw "comparison"; return ${expected}; }; export const result = worker();`;
  checkWorkerResult(name, source, expected);
}
for (const [name, expr, reason] of [
  ["object-primitive-equality", "({ valueOf() { return 1; } }) == 1", /an object is converted to a primitive/],
  ["host-equality-typeof", '"undefined" == typeof window', /unsupported global window/],
  ["host-typeof", '"undefined" != typeof window', /unsupported global window/],
]) {
  if (!includesCase(name)) continue;
  const result = workerSplit(name, `const worker = () => ${expr}; export const result = worker();`);
  if (assertRejectedOrOrderKeptSlice(result, reason)) {
    const expected = name === "host-typeof" ? false : true;
    assert.deepEqual(observe(result.files.sourceFile, "result"), { result: expected });
    assert.deepEqual(observe(path.join(result.out, "entry.mjs"), "result"), { result: expected });
  }
}
