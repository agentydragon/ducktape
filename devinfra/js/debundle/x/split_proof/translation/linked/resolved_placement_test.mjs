import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";
import {
  assertPublicEmissionRefused,
  assertPublicInputRefused,
  assertPublicRecheckRefused,
} from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { prepareLinkedFixture } from "./fixture_preview.mjs";
import { moduleEvaluationOrder } from "./harness.mjs";

// The modules import each other. Creating read only captures value's live cell;
// the call to read observes the cell before value receives its initializer.
const source = `var read = () => typeof value;
  const before = read();
  var value = () => read();
  export { read, before, value };`;
const spec = {
  chunk_id: "app",
  source_sha256: createHash("sha256").update(source).digest("hex"),
  import_order: ["value", "reader"],
  units: ["reader", "reader", "value", null].map((owner, ordinal) => ({ owner, ordinal })),
};
const valid = prepareLinkedFixture("resolved-cycle", source, spec);
assertPublicEmissionRefused(valid.manifest, valid.out);

// Visiting value first follows its request for reader. It does not evaluate value first.
await withSplitPreview({ manifest: valid.manifest }, async ({ directory, entryFile }) => {
  assert.deepEqual(moduleEvaluationOrder(directory, "source.mjs"), ["reader.mjs", "value.mjs", "source.mjs"]);
  for (const file of [valid.sourceFile, entryFile]) {
    const { before, read } = await import(pathToFileURL(file).href);
    assert.equal(before, "undefined");
    assert.equal(read(), "function");
  }
  assertPublicEmissionRefused(valid.manifest, directory);
  assertPublicRecheckRefused(valid.manifest, directory);
});

// Reversing just the import priority changes the cycle traversal and initialization.
const reverse = prepareLinkedFixture("resolved-reverse", source, { ...spec, import_order: ["reader", "value"] });
assertPublicEmissionRefused(reverse.manifest, reverse.out);
await withSplitPreview({ manifest: reverse.manifest, expectedExit: 1 }, async ({ directory, entryFile }) => {
  assert.deepEqual(moduleEvaluationOrder(directory, "source.mjs"), ["value.mjs", "reader.mjs", "source.mjs"]);
  const { before, read } = await import(pathToFileURL(entryFile).href);
  assert.equal(before, "function");
  assert.equal(read(), "function");
});

const incompatibleOrder = prepareLinkedFixture("resolved-sequential", source, {
  entry: "source.mjs",
  modules: { "source.mjs": ["#3"], "reader.mjs": ["#0", "#1"], "value.mjs": ["#2"] },
  order: ["value.mjs", "reader.mjs"],
  sequential: true,
});
assertPublicInputRefused(
  incompatibleOrder.manifest,
  incompatibleOrder.out,
  /source placement or module linkage checks failed/
);

for (const [name, invalid, why] of [
  ["hash", { ...spec, source_sha256: "0".repeat(64) }, /source_sha256/],
  ["unit-count", { ...spec, units: spec.units.slice(1) }, /unit count/],
  ["field", { ...spec, sequential: true }, /unexpected resolved placement field/],
]) {
  const { manifest, out } = prepareLinkedFixture(`resolved-invalid-${name}`, source, invalid);
  assertPublicInputRefused(manifest, out, why);
}
