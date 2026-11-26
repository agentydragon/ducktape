import assert from "node:assert/strict";
import { copyFileSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import {
  assertLinkedPreviewResult,
  assertMatchedModel,
  expectedModelObservations,
  prepareWorkerFixture,
} from "./fixture_preview.mjs";
import { caseShard, observe, prepare, root } from "./harness.mjs";

const runCase = caseShard();
if (runCase("stable-sites")) {
  const fixture = prepareWorkerFixture(
    "stable-template-sites",
    "function render(tag) { return [tag`same`, tag`same`]; }\n" +
      `const identity = value => value;
       export const result = (() => {
         const a = render(identity), b = render(identity);
         if (a[0] !== b[0] || a[1].raw !== b[1].raw || a[0] === a[1] || a[0].raw === a[1].raw)
           throw "template site identity";
         let failures = 0;
         try { a[0][0] = "changed"; } catch { failures++; }
         try { a[0].raw.extra = 1; } catch { failures++; }
         try { delete a[0].raw; } catch { failures++; }
         if (failures !== 3) throw "unfrozen template";
         return a[0];
       })();`,
    ["#1", "#2"]
  );
  const { manifest, out, sourceFile } = fixture;
  assertPublicEmissionRefused(manifest, out);
  await withSplitPreview({ manifest }, async ({ directory, entryFile }) => {
    assertMatchedModel(fixture, directory, [[["result", "0"], "same"]]);
    for (const file of [sourceFile, entryFile]) {
      const { result } = await import(pathToFileURL(file).href);
      assert.deepEqual(result, ["same"]);
      assert(Object.isFrozen(result) && Object.isFrozen(result.raw));
      assert.deepEqual(Object.getOwnPropertyDescriptor(result, "raw"), {
        value: result.raw,
        enumerable: false,
        configurable: false,
        writable: false,
      });
    }
  });
}

if (runCase("imported-tag")) {
  const dir = path.join(root, "imported-template-tag");
  const files = prepare(dir, {
    source: 'import { tag } from "./provider.mjs"; const value = tag`a\\x62${7}c`; export const result = value;',
    provider: `export function tag(strings) {
      const copy = Array.from(typeof strings === "string" ? [strings] : strings);
      if (copy === strings || copy.length !== 2 || copy[0] !== "ab" ||
          strings.raw[0] !== "a\\\\x62" || arguments.length !== 2 || arguments[1] !== 7) throw "tag";
      return copy[0] + arguments[1] + copy[1];
    }`,
    spec: { entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#1", "#2"] }, order: [], sequential: true },
  });
  const out = path.join(dir, "out");
  const { sourceFile, specFile, providerFile } = files;
  const manifest = writeDeployment(sourceFile, specFile, out);
  assertPublicEmissionRefused(manifest, out);
  assert.deepEqual(observe(sourceFile, "result"), { result: "ab7c" });
  await withSplitPreview({ manifest }, async ({ directory, entryFile }) => {
    // The unchanged provider lives beside the original fixture and is shared by the model pair.
    copyFileSync(providerFile, path.join(directory, "provider.mjs"));
    assert.deepEqual(observe(entryFile, "result"), { result: "ab7c" });
    const options = {
      source: sourceFile,
      spec: specFile,
      directory,
      providers: ["provider.mjs"],
      host: { profile: "browser-page" },
      observe: [["result"]],
    };
    const model = probeLinkedModel(options);
    assert.equal(model.status, "matched", JSON.stringify(model));
    assert(model.fuel > 0);
    assert.deepEqual(model.observed, expectedModelObservations([[["result"], "ab7c"]]));
    assert(
      model.assumptions.some((a) => a.includes("intrinsic Array constructor")),
      JSON.stringify(model)
    );
    assert(
      model.assumptions.some((a) => a.includes("intrinsic Array.from function")),
      JSON.stringify(model)
    );

    const printed = readFileSync(entryFile, "utf8");
    const changed = printed.replace("a\\x62", "ab");
    assert.notEqual(changed, printed);
    writeFileSync(entryFile, changed);
    const mutated = probeLinkedModel(options);
    assert.equal(mutated.status, "refused", JSON.stringify(mutated));
    assert.equal(mutated.reason_code, "linked_input", JSON.stringify(mutated));
    assert.match(mutated.reason, /emitted syntax differs from original unit 1/);
    await assert.rejects(import(pathToFileURL(entryFile).href), (reason) => reason === "tag");
    assertPublicEmissionRefused(manifest, directory);
    assertPublicRecheckRefused(manifest, directory);
  });
}

if (runCase("order-and-invalid-escape")) {
  const source = [
    "const run = () => {",
    '  const trace = { log: "" };',
    '  const receiver = { get tag() { trace.log += "g"; return function (s, v) {',
    '    if (this !== receiver || s[0] !== undefined || s.raw[0] !== "\\\\xZ") throw "receiver or cooked";',
    '    trace.log += "t"; return v; }; } };',
    '  const step = () => { trace.log += "s"; return 4; };',
    "  const value = receiver.tag`\\xZ${step()}`;",
    '  try { receiver.tag`a${(() => { throw 1; })()}`; } catch { trace.log += "c"; }',
    '  if (trace.log !== "gstgc") throw "order";',
    "  return [value, trace.log]; };",
    "export const result = run();",
  ].join("\n");
  const fixture = prepareWorkerFixture("tag-order", source);
  assertPublicEmissionRefused(fixture.manifest, fixture.out);
  await assertLinkedPreviewResult(
    fixture,
    [4, "gstgc"],
    [
      [["result", "0"], 4],
      [["result", "1"], "gstgc"],
    ]
  );
}
