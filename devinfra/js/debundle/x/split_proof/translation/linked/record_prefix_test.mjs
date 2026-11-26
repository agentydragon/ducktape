import assert from "node:assert/strict";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { assertMatchedModel, prepareLinkedFixture } from "./fixture_preview.mjs";
import { observe } from "./harness.mjs";

for (const [name, object, action, expected, previewExpected] of [
  ["alias-read", "{value: 1}", "alias.value;", 1, 1],
  ["alias-write", "{value: 1}", "alias.value = 2;", 2, 1],
  ["getter", "{get value() { return 3; }}", "alias.value;", 3, 3],
  ["inherited", "{__proto__: {value: 4}}", "alias.value;", 4, 4],
]) {
  const test = prepareLinkedFixture(
    name,
    `const table = ${object}; const alias = table; ${action} const chosen = table.value; export { table, chosen };`,
    { entry: "entry.mjs", modules: { "entry.mjs": ["#1", "#2", "#4"], "records.mjs": ["#0", "#3"] } }
  );
  assertPublicEmissionRefused(test.manifest, test.out);
  assert.deepEqual(observe(test.sourceFile, "chosen"), { chosen: expected });
  await withSplitPreview({ manifest: test.manifest, expectedExit: 1 }, ({ directory, entryFile }) => {
    assert.deepEqual(observe(entryFile, "chosen"), { chosen: previewExpected });
    if (name === "alias-write") {
      const model = probeLinkedModel({
        source: test.sourceFile,
        spec: test.specFile,
        directory,
        observe: [["chosen"]],
      });
      assert.equal(model.status, "refused", JSON.stringify(model));
      assert.equal(model.reason_code, "probe_mismatch");
      assert.deepEqual(model.observation, { kind: "value", path: ["chosen"], original: "2.000000", split: "1.000000" });
    } else {
      // A concrete model observation, not an all-world certificate.
      assertMatchedModel(test, directory, [[["chosen"], expected]]);
    }
    assertPublicEmissionRefused(test.manifest, directory);
    assertPublicRecheckRefused(test.manifest, directory);
  });
}

// The source initializes the producer first; the diagnostic split reads it too early.
{
  const test = prepareLinkedFixture(
    "before-initialization",
    "const table = {value: 1}; const chosen = table.value; export {table, chosen};",
    { entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#2"], "records.mjs": ["#1"] } }
  );
  assertPublicEmissionRefused(test.manifest, test.out);
  assert.deepEqual(observe(test.sourceFile, "chosen"), { chosen: 1 });
  await withSplitPreview({ manifest: test.manifest, expectedExit: 1 }, ({ directory, entryFile }) => {
    assert.deepEqual(observe(entryFile, "chosen"), { error: "ReferenceError" });
    const model = probeLinkedModel({ source: test.sourceFile, spec: test.specFile, directory, observe: [["chosen"]] });
    assert.equal(model.status, "refused", JSON.stringify(model));
    assert.equal(model.reason_code, "probe_throw");
    assert.equal(model.reason, "split threw during linked initialization");
    assertPublicEmissionRefused(test.manifest, directory);
    assertPublicRecheckRefused(test.manifest, directory);
  });
}
