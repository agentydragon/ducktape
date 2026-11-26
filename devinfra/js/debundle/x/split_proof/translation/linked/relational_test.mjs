import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import {
  arrow,
  assertCertifiedResult,
  assertRejected,
  assertRejectedOrOrderKeptSlice,
  binary,
  block,
  call,
  exportNamed,
  forcePreview,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  ownerReaderSplit,
  primitive,
  program,
  returns,
  throws,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const owner = "owner.mjs";
const reader = "reader.mjs";
const state = "state.mjs";

const inBounds = `function inBounds(index, range) {
  return index >= 0 && index < range.limit;
}`;
const numeric = `${inBounds}
  var limit;
  limit = " 0x3 ";
  const result = inBounds(2, { limit });
  export { result };`;
const placement = { [owner]: ["#0"], [state]: ["#1", "#2"], [reader]: ["#3"], [entry]: ["#4"] };
assertCertifiedResult(ownerReaderSplit("eager-comparison", numeric, placement, [owner, state, reader]), true);

// The hoisted var is undefined in the forced order, so the comparison sees
// NaN and returns false. Both schedules complete normally.
const earlyOrder = [owner, reader, state];
const early = ownerReaderSplit("early-comparison", numeric, placement, earlyOrder);
assertRejected(early, /probe executions completed normally with different final states/);
const preview = path.join(early.dir, "preview");
forcePreview(early.files, preview);
assert.deepEqual(moduleEvaluationOrder(preview, entry), [...earlyOrder, entry]);
assert.deepEqual(observe(early.files.sourceFile, "result"), { result: true });
assert.deepEqual(observe(path.join(preview, entry), "result"), { result: false });

assertCertifiedResult(
  ownerReaderSplit(
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

const operators = [
  ["<", (a, b) => a < b],
  ["<=", (a, b) => a <= b],
  [">", (a, b) => a > b],
  [">=", (a, b) => a >= b],
];

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
      test: binary("!==", binary(operator, primitive(left), primitive(right)), literal(compare(left, right))),
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
assertCertifiedResult(ownerReaderSplit("generated-comparisons", generated), true);

const object = ownerReaderSplit(
  "object-coercion",
  `const compare = () => {
    const object = { valueOf: () => 1 };
    return object <= 1;
  };
  const result = compare();
  export { result };`
);
assert.deepEqual(observe(object.files.sourceFile, "result"), { result: true });
assertRejectedOrOrderKeptSlice(object, /an object is converted to a primitive/);

const supplementary = ownerReaderSplit(
  "supplementary-property",
  `const first = "𐀀";
    const text = "A😀\\ufe0fZ";
    const read = () => {
      const second = "𐀁";
      const keys = { 𐀀: 1, [second]: 2, "😀": 3 };
      if (text.length !== 5 || text[0] !== "A" || text["4"] !== "Z" || text[-0] !== "A") throw 1;
      if (text[3] !== "\\ufe0f" || text[5] !== undefined || text["04"] !== undefined || text["-0"] !== undefined) throw 2;
      if (!(first < "\\ue000") || !(first < second) || first === second || first + second !== "𐀀𐀁") throw 3;
      if (keys.𐀀 !== 1 || keys[first] !== 1 || keys[second] !== 2 || keys["😀"] !== 3) throw 4;
      return \`𐀂/\${first}/\${text}\`;
    };
    const result = read();
    export { result };`,
  { [owner]: ["#0", "#1"], [reader]: ["#2", "#3"], [entry]: ["#4"] }
);
assertCertifiedResult(supplementary, "𐀂/𐀀/A😀\ufe0fZ");
const checked = () =>
  lower(supplementary.files.sourceFile, supplementary.files.specFile, supplementary.out, ["--check-output"]);
assert.deepEqual(checked().verdict, supplementary.verdict);

const readerFile = path.join(supplementary.out, reader);
const readerSource = readFileSync(readerFile, "utf8");
const changedReader = readerSource.replace("𐀂", "𐀃");
assert.notEqual(changedReader, readerSource);
writeFileSync(readerFile, changedReader);
assert.deepEqual(observe(path.join(supplementary.out, entry), "result"), { result: "𐀃/𐀀/A😀\ufe0fZ" });
const changed = checked();
assert.equal(changed.verdict.status, "rejected", changed.log);

for (const index of [0, 1]) {
  const half = ownerReaderSplit(
    `supplementary-index-${index}`,
    `const read = () => "𐀀"[${index}] !== "\\ufffd"; const result = read(); export { result };`
  );
  assert.deepEqual(observe(half.files.sourceFile, "result"), { result: true });
  assertRejectedOrOrderKeptSlice(half, /string index reads an isolated UTF-16 surrogate/);
}

// Replacing the isolated surrogate with U+FFFD would flip the result.
for (const [label, value] of [
  ["literal", '"\\ud800"'],
  ["template", "`\\ud800`"],
]) {
  const isolated = ownerReaderSplit(
    `isolated-surrogate-${label}`,
    `const compare = () => ${value} < "\\ue000"; const result = compare(); export { result };`
  );
  assert.deepEqual(observe(isolated.files.sourceFile, "result"), { result: true });
  assertRejectedOrOrderKeptSlice(isolated, /isolated UTF-16 surrogate/);
}
