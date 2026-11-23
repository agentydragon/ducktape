import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  arrow,
  block,
  exportNamed,
  forcePreview,
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
} from "./testing/linked_programs.mjs";

const entry = "entry.mjs";
const worker = "worker.mjs";

function split(name, source, modules = { [worker]: ["#0"], [entry]: ["#1", "#2"] }, order = [worker]) {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider: "", spec: { entry, modules, order, sequential: true } });
  const out = path.join(dir, "out");
  return { files, dir, out, ...lower(files.sourceFile, files.specFile, out) };
}

function assertCertified({ files, verdict, log, out }, expected) {
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: expected }, log);
  }
}

function assertRejected({ verdict, log, out }, reason) {
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, reason, log);
  assert(!existsSync(out));
}

assertCertified(
  split(
    "length-offset",
    `const last = (index, values) => index === values.length - 1;
     const result = () => [last(2, [0, 0, 0]), last("2", [0, 0, 0]), last(2, { length: "3" })];
     export { result };`
  ),
  [true, false, true]
);

assertCertified(split("eager-subtraction", "const length = 5; const result = length - 1; export { result };"), 4);

assertCertified(
  split(
    "arithmetic-operand-order",
    `const run = (fail) => {
      let phase = "start";
      const left = () => {
        if (phase !== "start") throw "unexpected left operand";
        phase = "left";
        if (fail) throw "left";
        return 3;
      };
      const right = () => {
        if (phase !== "left") throw "unexpected right operand";
        phase = "right";
        return 7;
      };
      try {
        const product = left() * right();
        if (!fail && phase === "right" && product === 21) return product;
        throw "unexpected completion";
      } catch (error) {
        if (fail && error === "left" && phase === "left") return error;
        throw error;
      }
    };
    const result = () => [run(false), run(true)];
    export { result };`
  ),
  [21, "left"]
);

const binary = (operator, left, right) => ({ type: "BinaryExpression", operator, left, right });
const unary = (operator, argument) => ({ type: "UnaryExpression", operator, argument, prefix: true });
const array = (elements) => ({ type: "ArrayExpression", elements });

function primitive(value) {
  if (value === Infinity) return id("Infinity");
  if (typeof value === "number" && (value < 0 || Object.is(value, -0))) return unary("-", literal(-value));
  return literal(value);
}

function requireEqual(actual, expected) {
  return {
    type: "IfStatement",
    test: binary("!==", actual, literal(expected)),
    consequent: throws(literal("wrong arithmetic result")),
    alternate: null,
  };
}

// Check both value and type when calling the relocated closure. A zero is
// observed via its reciprocal so Node's JSON serialization preserves its sign.
const cases = [
  ["+", 0.1, 0.2, 0.30000000000000004],
  ["+", "amount:", 1e21, "amount:1e+21"],
  ["+", true, 2, 3],
  ["+", null, "x", "nullx"],
  ["-", "\ufeff 0x10\t", true, 15],
  ["-", "bad", 1, NaN],
  ["*", null, Infinity, NaN],
  ["*", -0, 2, -0],
  ["/", 1, -0, -Infinity],
  ["/", "2.5e1", " 2 ", 12.5],
  ["%", -5.5, 2, -1.5],
  ["%", -4, 2, -0],
  ["%", 7, Infinity, 7],
];
const assertions = [];
const observations = [];
const expected = [];
for (const [index, [operator, a, b, value]] of cases.entries()) {
  const result = `value${index}`;
  const text = `text${index}`;
  const observable = value === 0 ? binary("/", literal(1), id(result)) : id(result);
  const string = String(value === 0 ? 1 / value : value);
  assertions.push(
    variable("const", result, binary(operator, primitive(a), primitive(b))),
    requireEqual(unary("typeof", id(result)), typeof value),
    variable("const", text, binary("+", literal(""), observable)),
    requireEqual(id(text), string)
  );
  observations.push(id(text));
  expected.push(string);
}
const generated = program([
  variable("const", "run", arrow([], block([...assertions, returns(array(observations))]))),
  variable("const", "result", id("run")),
  exportNamed([["result", "result"]]),
]);
assertCertified(split("generated-arithmetic", generated), expected);

// CoreJS cannot run valueOf. It refuses the initialization even with a catch
// that handles the actual JavaScript exception.
const conversion = split(
  "throws-in-object-coercion",
  `const run = () => {
    const object = { valueOf: () => { throw "conversion"; } };
    try { return object - 1; } catch (error) { return error; }
  };
  const result = run();
  export { result };`
);
assert.deepEqual(observe(conversion.files.sourceFile, "result"), { result: "conversion" });
assertRejected(conversion, /an object is converted to a primitive/);

const bigint = split("mixed-bigint", "const run = () => 1n + 1; const result = run(); export { result };");
assert.deepEqual(observe(bigint.files.sourceFile, "result"), { error: "TypeError" });
assertRejected(bigint, /unsupported literal/);

// An early read sees the already allocated var cell's undefined value, so both
// schedules complete. The arithmetic exposes the different initialization order.
{
  const source = `var factor;
    factor = 2;
    const result = 6 * factor === 12;
    export { result };`;
  const reader = "reader.mjs";
  const modules = { [worker]: ["#0", "#1"], [reader]: ["#2"], [entry]: ["#3"] };
  assertCertified(split("ordered-arithmetic", source, modules, [worker, reader]), true);
  const early = split("early-arithmetic", source, modules, [reader, worker]);
  assertRejected(early, /probe executions completed normally with different final states/);
  const preview = path.join(early.dir, "preview");
  forcePreview(early.files, preview);
  assert.deepEqual(moduleEvaluationOrder(preview, entry), [reader, worker, entry]);
  assert.deepEqual(observe(early.files.sourceFile, "result"), { result: true });
  assert.deepEqual(observe(path.join(preview, entry), "result"), { result: false });
}
