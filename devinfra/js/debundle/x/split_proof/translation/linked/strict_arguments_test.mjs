import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { assertMatchedModel, prepareWorkerFixture } from "./fixture_preview.mjs";
import { observe } from "./harness.mjs";

const fixture = prepareWorkerFixture(
  "reached-strict-arguments",
  `function inspect(first, second) {
     const original = arguments;
     const read = () => arguments;
     first = 12;
     arguments[1] = 7;
     const inner = function () { return arguments; };
     const nested = inner(8);
     let poisoned = false;
     try { original.callee; } catch { poisoned = true; }
     if (!poisoned || original !== read() || nested === original || nested.length !== 1 ||
         nested[0] !== 8 || first !== 12 || second !== 3 || original[0] !== 2 ||
         original[1] !== 7 || original.length !== 3 || original[2] !== 4) throw "arguments";
     original.length = 2.5;
     const items = [...original];
     if (items.length !== 2 || items[0] !== 2 || items[1] !== 7) throw "array-like iterator";
     original.length = 3;
     return read;
   }
   export const result = (() => {
     const a = inspect(2, 3, 4), b = inspect(2, 3, 4);
     if (a() === b()) throw "reused arguments";
     return [a().length, a()[0], a()[1], b()[2]];
   })();`
);
const { manifest, out, sourceFile, specFile } = fixture;
const expected = [3, 2, 7, 4];
assertPublicEmissionRefused(manifest, out);
assert.deepEqual(observe(sourceFile, "result"), { result: expected });
await withSplitPreview({ manifest }, ({ directory, entryFile }) => {
  assert.deepEqual(observe(entryFile, "result"), { result: expected });
  assertMatchedModel(
    fixture,
    directory,
    expected.map((value, index) => [["result", String(index)], value])
  );

  const worker = path.join(directory, "worker.mjs");
  const source = readFileSync(worker, "utf8");
  const changed = source.replace("arguments[1]", "arguments[0]");
  assert.notEqual(changed, source);
  writeFileSync(worker, changed);
  const model = probeLinkedModel({ source: sourceFile, spec: specFile, directory });
  assert.equal(model.status, "refused", JSON.stringify(model));
  assert.equal(model.reason_code, "linked_input", JSON.stringify(model));
  assert.match(model.reason, /emitted syntax differs from original unit 0/);

  assertPublicEmissionRefused(manifest, directory);
  assertPublicRecheckRefused(manifest, directory);
});
