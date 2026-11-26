import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { assertLinkedPreviewResult, assertMatchedModel, prepareLinkedFixture } from "./fixture_preview.mjs";
import {
  arrow,
  binary,
  block,
  call,
  exportNamed,
  id,
  literal,
  moduleEvaluationOrder,
  observe,
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

function prepareComparison(
  name,
  source,
  modules = { [owner]: ["#0"], [reader]: ["#1"], [entry]: ["#2"] },
  order = [owner, reader]
) {
  return prepareLinkedFixture(name, source, { entry, modules, order, sequential: true });
}

async function checkComparison(fixture, expected) {
  assertPublicEmissionRefused(fixture.manifest, fixture.out);
  await assertLinkedPreviewResult(fixture, expected);
}

async function checkModelRefusal({ manifest, out, sourceFile, specFile }, reason) {
  assertPublicEmissionRefused(manifest, out);
  assert.deepEqual(observe(sourceFile, "result"), { result: true });
  await withSplitPreview({ manifest }, ({ directory, entryFile }) => {
    assert.deepEqual(observe(entryFile, "result"), { result: true });
    const model = probeLinkedModel({ source: sourceFile, spec: specFile, directory });
    assert.equal(model.status, "refused", JSON.stringify(model));
    assert.match(model.reason, reason, JSON.stringify(model));
  });
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
await checkComparison(prepareComparison("eager-comparison", numeric, placement, [owner, state, reader]), true);

// The hoisted var is undefined in the forced order, so the comparison sees
// NaN and returns false. Both schedules complete normally.
const earlyOrder = [owner, reader, state];
const early = prepareComparison("early-comparison", numeric, placement, earlyOrder);
assertPublicEmissionRefused(early.manifest, early.out);
assert.deepEqual(observe(early.sourceFile, "result"), { result: true });
await withSplitPreview({ manifest: early.manifest, expectedExit: 1 }, ({ directory, entryFile }) => {
  assert.deepEqual(moduleEvaluationOrder(directory, entry), [...earlyOrder, entry]);
  assert.deepEqual(observe(entryFile, "result"), { result: false });
  const model = probeLinkedModel({
    source: early.sourceFile,
    spec: early.specFile,
    directory,
    observe: [["result"]],
  });
  assert.equal(model.status, "refused", JSON.stringify(model));
  assert.equal(model.reason_code, "probe_mismatch", JSON.stringify(model));
  assert.deepEqual(model.observation, { kind: "value", path: ["result"], original: "true", split: "false" });
});

await checkComparison(
  prepareComparison(
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
await checkComparison(prepareComparison("generated-comparisons", generated), true);

const object = prepareComparison(
  "object-coercion",
  `const compare = () => {
    const object = { valueOf: () => 1 };
    return object <= 1;
  };
  const result = compare();
  export { result };`
);
await checkModelRefusal(object, /an object is converted to a primitive/);

const supplementary = prepareComparison(
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
const { manifest, out, sourceFile, specFile } = supplementary;
const expected = "𐀂/𐀀/A😀\ufe0fZ";
assertPublicEmissionRefused(manifest, out);
assert.deepEqual(observe(sourceFile, "result"), { result: expected });
await withSplitPreview({ manifest }, ({ directory, entryFile }) => {
  assert.deepEqual(observe(entryFile, "result"), { result: expected });
  assertMatchedModel(supplementary, directory, [[["result"], expected]]);
  const readerFile = path.join(directory, reader);
  const readerSource = readFileSync(readerFile, "utf8");
  const changed = readerSource.replace("𐀂", "𐀃");
  assert.notEqual(changed, readerSource);
  writeFileSync(readerFile, changed);
  assert.deepEqual(observe(entryFile, "result"), { result: "𐀃/𐀀/A😀\ufe0fZ" });
  const model = probeLinkedModel({ source: sourceFile, spec: specFile, directory });
  assert.equal(model.status, "refused", JSON.stringify(model));
  assert.equal(model.reason_code, "linked_input", JSON.stringify(model));
  assert.match(model.reason, /emitted syntax differs from original unit 2/);
  assertPublicEmissionRefused(manifest, directory);
  assertPublicRecheckRefused(manifest, directory);
});

for (const index of [0, 1]) {
  const half = prepareComparison(
    `supplementary-index-${index}`,
    `const read = () => "𐀀"[${index}] !== "\\ufffd"; const result = read(); export { result };`
  );
  await checkModelRefusal(half, /string index reads an isolated UTF-16 surrogate/);
}

// Replacing the isolated surrogate with U+FFFD would flip the result.
for (const [label, value] of [
  ["literal", '"\\ud800"'],
  ["template", "`\\ud800`"],
]) {
  const isolated = prepareComparison(
    `isolated-surrogate-${label}`,
    `const compare = () => ${value} < "\\ue000"; const result = compare(); export { result };`
  );
  await checkModelRefusal(isolated, /isolated UTF-16 surrogate/);
}
