import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { assertMatchedModel, prepareWorkerFixture } from "./fixture_preview.mjs";
import { observe } from "./harness.mjs";

const source = String.raw`const make = () => /[a/]+\s/ymi;
const check = () => {
  const a = make(), b = make();
  if (a === b || a.lastIndex !== 0 || b.lastIndex !== 0) throw "identity";
  a.lastIndex = 4;
  if (a.lastIndex !== 4 || b.lastIndex !== 0 || {...a}.lastIndex !== undefined) throw "lastIndex";
  if (a.source !== "[a/]+\\s" || a.flags !== "imy" || !a.sticky || a.global) throw "slots";
  return true;
};
export const result = check();`;
const fixture = prepareWorkerFixture("regexp-literal", source, ["#1", "#2"]);
const { manifest, out, sourceFile, specFile } = fixture;
assertPublicEmissionRefused(manifest, out);
assert.deepEqual(observe(sourceFile, "result"), { result: true });
await withSplitPreview({ manifest }, ({ directory, entryFile }) => {
  assert.deepEqual(observe(entryFile, "result"), { result: true });
  assertMatchedModel(fixture, directory, [[["result"], true]]);
  const file = path.join(directory, "worker.mjs");
  const original = readFileSync(file, "utf8");
  for (const changed of [original.replace("[a/]", "[b/]"), original.replace("ymi", "ym")]) {
    assert.notEqual(changed, original);
    writeFileSync(file, changed);
    const model = probeLinkedModel({ source: sourceFile, spec: specFile, directory });
    assert.equal(model.status, "refused", JSON.stringify(model));
    assert.equal(model.reason_code, "linked_input", JSON.stringify(model));
    assert.match(model.reason, /emitted syntax differs from original unit 0/);
    assertPublicEmissionRefused(manifest, directory);
    assertPublicRecheckRefused(manifest, directory);
  }
});

for (const [method, result] of [
  ["exec", ["a"]],
  ["test", true],
]) {
  const { manifest, out, sourceFile, specFile } = prepareWorkerFixture(
    `regexp-${method}`,
    `const rx = /a/g; export const result = rx.${method}("a");`
  );
  assertPublicEmissionRefused(manifest, out);
  assert.deepEqual(observe(sourceFile, "result"), { result });
  await withSplitPreview({ manifest }, ({ directory, entryFile }) => {
    assert.deepEqual(observe(entryFile, "result"), { result });
    const model = probeLinkedModel({ source: sourceFile, spec: specFile, directory });
    assert.equal(model.status, "refused", JSON.stringify(model));
    assert(model.reason.includes(`RegExp.prototype.${method}`), JSON.stringify(model));
  });
}
