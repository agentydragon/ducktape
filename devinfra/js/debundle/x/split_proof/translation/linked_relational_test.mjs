import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  arrow,
  block,
  call,
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
} from "./linked_programs_test_support.mjs";

const entry = "entry.mjs";
const owner = "owner.mjs";
const reader = "reader.mjs";
const state = "state.mjs";

function split(
  name,
  source,
  modules = { [owner]: ["#0"], [reader]: ["#1"], [entry]: ["#2"] },
  order = [owner, reader]
) {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider: "", spec: { entry, modules, order, sequential: true } });
  const out = path.join(dir, "out");
  return { files, dir, out, ...lower(files.sourceFile, files.specFile, out) };
}

function assertCertified({ files, verdict, log, out }, result) {
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", log);
  assert.deepEqual(observe(files.sourceFile, "result"), { result });
  assert.deepEqual(observe(path.join(out, entry), "result"), { result });
}

function assertRejected({ verdict, log, out }, reason) {
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, reason, log);
  assert(!existsSync(out));
}

const inBounds = `function inBounds(index, range) {
  return index >= 0 && index < range.limit;
}`;
const numeric = `${inBounds}
  var limit;
  limit = " 0x3 ";
  const result = inBounds(2, { limit });
  export { result };`;
const placement = { [owner]: ["#0"], [state]: ["#1", "#2"], [reader]: ["#3"], [entry]: ["#4"] };
assertCertified(split("eager-comparison", numeric, placement, [owner, state, reader]), true);

// The hoisted var is undefined in the forced order, so the comparison sees
// NaN and returns false. Both schedules complete normally.
const earlyOrder = [owner, reader, state];
const early = split("early-comparison", numeric, placement, earlyOrder);
assertRejected(early, /probe executions completed normally with different final states/);
const preview = path.join(early.dir, "preview");
forcePreview(early.files, preview);
assert.deepEqual(moduleEvaluationOrder(preview, entry), [...earlyOrder, entry]);
assert.deepEqual(observe(early.files.sourceFile, "result"), { result: true });
assert.deepEqual(observe(path.join(preview, entry), "result"), { result: false });

assertCertified(
  split(
    "greater-or-equal-operand-order",
    `const compare = () => {
      let n = 0;
      const next = () => { n = n + 1; return n; };
      return next() >= next();
    };
    const result = compare();
    export { result };`
  ),
  false
);

const binary = (operator, left, right) => ({ type: "BinaryExpression", operator, left, right });
const operators = [
  ["<", (a, b) => a < b],
  ["<=", (a, b) => a <= b],
  [">", (a, b) => a > b],
  [">=", (a, b) => a >= b],
];
function operand(value) {
  if (value === undefined || Number.isNaN(value) || value === Infinity) return id(String(value));
  if (Object.is(value, -0)) return { type: "UnaryExpression", operator: "-", prefix: true, argument: literal(0) };
  return literal(value);
}

// The checked eager call has to reach every assertion to complete normally.
// U+10000 sorts before U+E000 in UTF-16 and after it in code-point order.
const assertions = [];
for (const [left, right] of [
  ["", "a"],
  ["aa", "a"],
  ["10", "2"],
  ["𐀀", "\ue000"],
  ["𐀀", "𐀁"],
  ["𐀀", "𐀀"],
  [" 0b10 ", true],
  [null, -0],
  [Infinity, "Infinity"],
  [undefined, 1],
  [1, NaN],
  ["bad", 1],
]) {
  for (const [operator, compare] of operators) {
    assertions.push({
      type: "IfStatement",
      test: binary("!==", binary(operator, operand(left), operand(right)), literal(compare(left, right))),
      consequent: throws(literal("wrong comparison result")),
      alternate: null,
    });
  }
}
const generated = program([
  variable("const", "validate", arrow([], block([...assertions, returns(literal(true))]))),
  variable("const", "result", call(id("validate"))),
  exportNamed([["result", "result"]]),
]);
assertCertified(split("generated-comparisons", generated), true);

const object = split(
  "object-coercion",
  `const compare = () => {
    const object = { valueOf: () => 1 };
    return object <= 1;
  };
  const result = compare();
  export { result };`
);
assert.deepEqual(observe(object.files.sourceFile, "result"), { result: true });
assertRejected(object, /an object is converted to a primitive/);

const supplementary = split(
  "supplementary-property",
  'const length = () => "𐀀".length; const result = length(); export { result };'
);
assert.deepEqual(observe(supplementary.files.sourceFile, "result"), { result: 2 });
assertRejected(supplementary, /string outside the Basic Multilingual Plane/);

// Replacing the isolated surrogate with U+FFFD would flip the result.
for (const [label, value] of [
  ["literal", '"\\ud800"'],
  ["template", "`\\ud800`"],
]) {
  const isolated = split(
    `isolated-surrogate-${label}`,
    `const compare = () => ${value} < "\\ue000"; const result = compare(); export { result };`
  );
  assert.deepEqual(observe(isolated.files.sourceFile, "result"), { result: true });
  assertRejected(isolated, /isolated UTF-16 surrogate/);
}
