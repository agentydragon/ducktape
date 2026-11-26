import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { assertLinkedPreviewResult, expectedModelObservations, prepareLinkedFixture } from "./fixture_preview.mjs";
import { observe } from "./harness.mjs";

const host = { profile: "browser-page" };

function fixture(name, source) {
  return prepareLinkedFixture(name, source, {
    entry: "entry.mjs",
    modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
    order: ["worker.mjs"],
    sequential: true,
  });
}

const brand = fixture(
  "symbol-brand",
  `const brand = Symbol("RecordBrand");
  const result = (() => {
    let key = brand;
    const Box = class { [key] = true; };
    key = Symbol("RecordBrand");
    const a = new Box(), b = new Box();
    if (typeof brand !== "symbol" || key === brand || Symbol() === Symbol()) throw "freshness";
    if (a[brand] !== true || b[brand] !== true || key in a || "RecordBrand" in a) throw "field key";
    if (brand in Box.prototype || brand in Box) throw "field owner";
    delete a[brand];
    if (brand in a || b[brand] !== true) throw "per-instance descriptor";
    return true;
  })();
  export { result };`
);
const { manifest, out, sourceFile, specFile } = brand;
assertPublicEmissionRefused(manifest, out);
assert.deepEqual(observe(sourceFile, "result"), { result: true });
await withSplitPreview({ manifest }, ({ directory, entryFile }) => {
  assert.deepEqual(observe(entryFile, "result"), { result: true });
  const options = { source: sourceFile, spec: specFile, directory, observe: [["result"]] };
  const model = probeLinkedModel({ ...options, host });
  assert.equal(model.status, "matched", JSON.stringify(model));
  assert(model.fuel > 0);
  assert.deepEqual(model.observed, expectedModelObservations([[["result"], true]]));
  assert.deepEqual(model.assumptions, [
    "Symbol returns the intrinsic Symbol function without observable effects throughout initialization",
  ]);

  const owner = path.join(directory, "worker.mjs");
  const original = readFileSync(owner, "utf8");
  const changed = original.replace(/Symbol\(["']RecordBrand["']\)/, '"RecordBrand"');
  assert.notEqual(changed, original);
  writeFileSync(owner, changed);
  const mutated = probeLinkedModel({ ...options, host });
  assert.equal(mutated.status, "refused", JSON.stringify(mutated));
  assert.equal(mutated.reason_code, "linked_input", JSON.stringify(mutated));
  assert.match(mutated.reason, /emitted syntax differs from original unit 0/);
  assertPublicEmissionRefused(manifest, directory);
  assertPublicRecheckRefused(manifest, directory);
});

const shadow = fixture(
  "shadowed-symbol",
  `const Symbol = name => name;
  const result = (() => {
    const key = Symbol("local");
    const Box = class { [key] = 42; };
    return new Box().local;
  })();
  export { result };`
);
assertPublicEmissionRefused(shadow.manifest, shadow.out);
await assertLinkedPreviewResult(shadow, 42);

const namedField = fixture(
  "computed-field-function-name",
  `const brand = Symbol("callback");
  class Box { [brand] = () => 1; }
  export { Box };`
);
assertPublicEmissionRefused(namedField.manifest, namedField.out);
await withSplitPreview({ manifest: namedField.manifest }, async ({ directory, entryFile }) => {
  for (const file of [namedField.sourceFile, entryFile]) {
    const { Box } = await import(pathToFileURL(file).href);
    const instance = new Box();
    const [key] = Object.getOwnPropertySymbols(instance);
    assert.equal(key.description, "callback");
    assert.equal(instance[key].name, "[callback]");
    assert.equal(instance[key](), 1);
  }
  const model = probeLinkedModel({ source: namedField.sourceFile, spec: namedField.specFile, directory, host });
  assert.equal(model.status, "refused", JSON.stringify(model));
  assert.match(model.reason, /unsupported function or class named by a computed field key/);
});
