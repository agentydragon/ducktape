import assert from "node:assert/strict";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { assertLinkedPreviewResult, assertMatchedModel, prepareLinkedFixture } from "./fixture_preview.mjs";
import {
  arrow,
  block,
  call,
  exportNamed,
  expression,
  id,
  literal,
  moduleEvaluationOrder,
  observe,
  program,
  returns,
  throws,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const owner = "owner.mjs";
const reader = "reader.mjs";
const state = "state.mjs";

function prepareSwitch(
  name,
  source,
  modules = { [owner]: ["#0"], [reader]: ["#1"], [entry]: ["#2"] },
  order = [owner, reader]
) {
  return prepareLinkedFixture(name, source, { entry, modules, order, sequential: true });
}

const source = `function choose() {
  switch (mode) {
    case "active": return "selected";
    default: return "empty";
  }
}
var mode;
mode = "active";
const result = choose();
export { result };`;
const placement = { [owner]: ["#0"], [state]: ["#1", "#2"], [reader]: ["#3"], [entry]: ["#4"] };
for (const early of [false, true]) {
  const order = early ? [owner, reader, state] : [owner, state, reader];
  const fixture = prepareSwitch(`switch-${early ? "early" : "ordered"}`, source, placement, order);
  const { manifest, out, sourceFile, specFile } = fixture;
  assertPublicEmissionRefused(manifest, out);
  assert.deepEqual(observe(sourceFile, "result"), { result: "selected" });
  await withSplitPreview({ manifest, expectedExit: early ? 1 : 0 }, ({ directory, entryFile }) => {
    assert.deepEqual(moduleEvaluationOrder(directory, entry), [...order, entry]);
    assert.deepEqual(observe(entryFile, "result"), { result: early ? "empty" : "selected" });
    if (early) {
      const model = probeLinkedModel({ source: sourceFile, spec: specFile, directory, observe: [["result"]] });
      assert.equal(model.status, "refused", JSON.stringify(model));
      assert.equal(model.reason_code, "probe_mismatch", JSON.stringify(model));
      assert.deepEqual(model.observation, {
        kind: "value",
        path: ["result"],
        original: '"selected"',
        split: '"empty"',
      });
    } else {
      assertMatchedModel(fixture, directory, [[["result"], "selected"]]);
    }
    assertPublicEmissionRefused(manifest, directory);
    assertPublicRecheckRefused(manifest, directory);
  });
}

const append = (suffix) => ({
  type: "AssignmentExpression",
  operator: "=",
  left: id("trace"),
  right: { type: "BinaryExpression", operator: "+", left: id("trace"), right: literal(suffix) },
});
const mark = (suffix, value) => ({ type: "SequenceExpression", expressions: [append(suffix), literal(value)] });
const clause = (test, ...consequent) => ({ type: "SwitchCase", test, consequent });
const generated = program([
  variable(
    "const",
    "choose",
    arrow(
      [],
      block([
        variable("let", "trace", literal("")),
        {
          type: "SwitchStatement",
          discriminant: mark("d", "1"),
          cases: [
            clause(mark("n", 1), throws(literal("coerced case test"))),
            clause(mark("s", "1"), expression(append("b")), { type: "BreakStatement", label: null }),
            clause(mark("x", "1"), throws(literal("evaluated a later test"))),
            clause(null, throws(literal("entered default"))),
          ],
        },
        returns(id("trace")),
      ])
    )
  ),
  variable("const", "result", call(id("choose"))),
  exportNamed([["result", "result"]]),
]);
const generatedFixture = prepareSwitch("switch-generated-evaluation-order", generated);
assertPublicEmissionRefused(generatedFixture.manifest, generatedFixture.out);
await assertLinkedPreviewResult(generatedFixture, "dnsb");

const grouped = prepareSwitch(
  "switch-grouped-labels",
  `function choose(value) {
    let trace = "";
    switch ((trace += "d", value)) {
      case (trace += "a", "a"):
      case (trace += "b", "b"): {
        trace += "m";
        if (value === "a") { break; }
        trace += "b";
        break;
      }
      case (trace += "z", "z"):
      default: trace += "x";
    }
    return trace + "!";
  }
  const result = [choose("a"), choose("b"), choose("x"), choose("z")];
  export { result };`
);
const labels = ["dam!", "dabmb!", "dabzx!", "dabzx!"];
assertPublicEmissionRefused(grouped.manifest, grouped.out);
await assertLinkedPreviewResult(
  grouped,
  labels,
  labels.map((label, index) => [["result", String(index)], label])
);

const enclosing = prepareSwitch(
  "switch-keeps-loop-control",
  `function choose() {
    let trace = "";
    for (let i = 0; i < 3; i++) {
      switch (i) {
        case 0: {
          try { continue; } finally { trace += "f"; }
          break;
        }
        case 1: {
          for (let j = 0; j < 1; j++) { trace += "i"; break; }
          switch (1) { case 1: { trace += "s"; break; } }
          trace += "o";
          break;
        }
        default: {
          try { break; } finally { trace += "b"; }
          trace += "unreachable";
        }
      }
      trace += "a";
    }
    return trace;
  }
  const result = choose();
  export { result };`
);
assertPublicEmissionRefused(enclosing.manifest, enclosing.out);
await assertLinkedPreviewResult(enclosing, "fisoaba");

for (const [name, cases, result, reason] of [
  ["fallthrough", "case 0: 1; case 1: return 2;", 2, /unsupported switch fallthrough/],
  ["default-order", "default: return 0; case 1: return 1;", 0, /unsupported nontrailing switch default/],
  [
    "shared-lexical",
    "case 0: let shared = 1; break; case 1: return shared;",
    0,
    /unsupported switch lexical declaration/,
  ],
  [
    "inner-break-falls-through",
    "case 0: { while (true) break; } case 1: return 2;",
    2,
    /unsupported switch fallthrough/,
  ],
]) {
  const { manifest, out, sourceFile, specFile } = prepareSwitch(
    `switch-${name}`,
    `function choose(n) { switch (n) { ${cases} } return 0; }
     const result = choose(0); export { result };`
  );
  assertPublicEmissionRefused(manifest, out);
  assert.deepEqual(observe(sourceFile, "result"), { result });
  await withSplitPreview({ manifest }, ({ directory, entryFile }) => {
    assert.deepEqual(observe(entryFile, "result"), { result });
    const model = probeLinkedModel({ source: sourceFile, spec: specFile, directory });
    assert.equal(model.status, "refused", JSON.stringify(model));
    assert.match(model.reason, reason, JSON.stringify(model));
  });
}
