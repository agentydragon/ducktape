import assert from "node:assert/strict";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { assertLinkedPreviewResult, assertMatchedModel, prepareLinkedFixture } from "./fixture_preview.mjs";
import {
  arrow,
  block,
  call,
  caseShard,
  conditional,
  exportNamed,
  id,
  literal,
  moduleEvaluationOrder,
  observe,
  program,
  throws,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const includesCase = caseShard();

async function checkSequence(name, source, expected) {
  const fixture = prepareLinkedFixture(name, source, {
    entry,
    modules: { "worker.mjs": ["#0"], [entry]: ["#1", "#2"] },
    order: ["worker.mjs"],
    sequential: true,
  });
  assertPublicEmissionRefused(fixture.manifest, fixture.out);
  await assertLinkedPreviewResult(fixture, expected);
}

// Generate different operand counts and both direct and deferred sequences.
// The guard makes a wrong final value fail in the model even if both sides lower it the same way.
for (const [index, operands] of [
  [true, 0],
  [null, "ignored", false],
  [1, 2, 3, "last"],
  [...Array.from({ length: 89 }, (_, i) => i), "wide final value"],
].entries()) {
  if (!includesCase(`generated-sequence-${index}`)) continue;
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
  await checkSequence(`generated-sequence-${index}`, source, expected);
}

if (includesCase("first-abrupt-operand")) {
  await checkSequence(
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
}

// The member access provides a receiver only for the direct call.
if (includesCase("detached-receiver")) {
  await checkSequence(
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
}

// The sequence operand does not inherit the static property key as its function name.
if (includesCase("sequence-property-value")) {
  await checkSequence(
    "sequence-property-value",
    `const worker = { run: (0, () => 9) };
  const value = worker.run.name === "" ? worker.run() : (() => { throw "wrong function name"; })();
  export { value as result };`,
    9
  );
}

// The discarded read can still throw before the following operand. Moving the
// late declaration earlier changes which error reaches the catch.
for (const dependent of [false, true]) {
  if (!includesCase(`reordered-sequence-${dependent}`)) continue;
  const source = `const stop = () => { throw "stopped"; };
    const read = () => {
      try { return (${dependent ? "late" : "0"}, stop(), 1); }
      catch (error) { return typeof error; }
    };
    const value = read();
    const late = 2;
    export { value as result };`;
  const fixture = prepareLinkedFixture(`reordered-sequence-${dependent}`, source, {
    entry,
    modules: { "worker.mjs": ["#3"], [entry]: ["#0", "#1", "#2", "#4"] },
    order: ["worker.mjs"],
    sequential: true,
  });
  const { manifest, out, sourceFile, specFile } = fixture;
  const original = dependent ? "object" : "string";
  assertPublicEmissionRefused(manifest, out);
  assert.deepEqual(observe(sourceFile, "result"), { result: original });
  await withSplitPreview({ manifest, expectedExit: dependent ? 1 : 0 }, ({ directory, entryFile }) => {
    assert.deepEqual(moduleEvaluationOrder(directory, entry), ["worker.mjs", entry]);
    assert.deepEqual(observe(entryFile, "result"), { result: "string" });
    if (dependent) {
      const model = probeLinkedModel({ source: sourceFile, spec: specFile, directory, observe: [["result"]] });
      assert.equal(model.status, "refused", JSON.stringify(model));
      assert.equal(model.reason_code, "probe_mismatch", JSON.stringify(model));
      assert.deepEqual(model.observation, {
        kind: "value",
        path: ["result"],
        original: '"object"',
        split: '"string"',
      });
    } else {
      assertMatchedModel(fixture, directory, [[["result"], "string"]]);
    }
    assertPublicEmissionRefused(manifest, directory);
    assertPublicRecheckRefused(manifest, directory);
  });
}
