import assert from "node:assert/strict";
import path from "node:path";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import {
  arrow,
  binary,
  call,
  exportNamed,
  id,
  literal,
  member,
  moduleEvaluationOrder,
  observe,
  prepare,
  program,
  property,
  root,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const owner = "owner.mjs";
const observer = "observer.mjs";
const oneCall = { [owner]: ["#0"], [observer]: ["#1"], [entry]: ["#2"] };

function fixture(name, source, modules = oneCall, order = [owner, observer]) {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider: "", spec: { entry, modules, order, sequential: true } });
  return { files, out: path.join(dir, "public") };
}

async function checkPreview(
  test,
  result,
  {
    order = [owner, observer],
    original = result,
    expectedExit = 0,
    modeled = {
      status: "matched",
      assumptions: [],
      host_refs: [],
      observed: [{ path: ["result"], original: String(result), split: String(result) }],
    },
  } = {}
) {
  const { files, out } = test;
  const manifest = writeDeployment(files.sourceFile, files.specFile, out);
  assertPublicEmissionRefused(manifest, out);
  assertPublicRecheckRefused(manifest, out);
  assert.deepEqual(observe(files.sourceFile, "result"), { result: original });

  await withSplitPreview(
    { source: files.sourceFile, spec: files.specFile, expectedExit },
    ({ directory, entryFile }) => {
      assert.deepEqual(moduleEvaluationOrder(directory, entry), [...order, entry]);
      assert.deepEqual(observe(entryFile, "result"), { result });
      // Compare the linked model at the concrete probe’s chosen fuel.
      const { fuel, ...reply } = probeLinkedModel({
        source: files.sourceFile,
        spec: files.specFile,
        directory,
        observe: [["result"]],
      });
      assert.deepEqual(reply, modeled);
      if (modeled.status === "matched") assert(Number.isInteger(fuel) && fuel > 0, `invalid probe fuel ${fuel}`);

      assertPublicEmissionRefused(manifest, directory);
      assertPublicRecheckRefused(manifest, directory);
    }
  );
}

const position = `function position(index, values) {
  return values.length === 1 ? "single" : index === 0 ? "first" : index === values.length - 1 ? "last" : "middle";
}`;

// The exported Boolean records an eager call through the split's import.
await checkPreview(
  fixture(
    "position-observer",
    `${position}
    const result = position(2, { length: " 3 " }) === "last";
    export { result };`
  ),
  true
);

const lengthRead = member(id("values"), id("length"));

for (const [name, length, index, result] of [
  ["numeric-length", 3, 2, true],
  ["string-length", " 0x3 ", 2, true],
  ["strict-index", " 3 ", "2", false],
]) {
  const source = program([
    variable(
      "const",
      "isLast",
      arrow([id("index"), id("values")], binary("===", id("index"), binary("-", lengthRead, literal(1))))
    ),
    variable(
      "const",
      "result",
      call(id("isLast"), [
        literal(index),
        { type: "ObjectExpression", properties: [property("length", literal(length))] },
      ])
    ),
    exportNamed([["result", "result"]]),
  ]);
  await checkPreview(fixture(name, source), result);
}

// Both schedules finish. In the forced split, the eager call sees the hoisted
// length cell before the write, so its subtraction produces NaN.
{
  const source = `${position}
    var length;
    length = " 3 ";
    const result = position(2, { length }) === "last";
    export { result };`;
  const state = "state.mjs";
  const modules = { [owner]: ["#0"], [state]: ["#1", "#2"], [observer]: ["#3"], [entry]: ["#4"] };
  await checkPreview(fixture("after-length-write", source, modules, [owner, state, observer]), true, {
    order: [owner, state, observer],
  });

  const order = [owner, observer, state];
  const early = fixture("before-length-write", source, modules, order);
  const observation = { kind: "value", path: ["result"], original: "true", split: "false" };
  await checkPreview(early, false, {
    order,
    original: true,
    expectedExit: 1,
    modeled: {
      status: "refused",
      reason_code: "probe_mismatch",
      reason: "probe executions completed normally with different final states",
      observation,
    },
  });
}
