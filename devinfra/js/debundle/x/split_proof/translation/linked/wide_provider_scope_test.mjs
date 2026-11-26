import assert from "node:assert/strict";
import { copyFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { expectedModelObservations } from "./fixture_preview.mjs";
import { prepare, root } from "./harness.mjs";

// Each of the 512 import specifiers resolves a cell in the same provider scope.
const names = Array.from({ length: 512 }, (_, i) => "value" + i);
const provider = names.map((v, i) => "export const " + v + " = " + i + ";").join("\n");
const source =
  "import {" +
  names.join(",") +
  '} from "./provider.mjs";\n' +
  "const helper = () => 1; export const answer = value511 + helper();";
const dir = path.join(root, "wide-provider");
const files = prepare(dir, {
  source,
  provider,
  spec: {
    entry: "entry.mjs",
    modules: { "entry.mjs": ["#0", "#2"], "helper.mjs": ["#1"] },
    order: ["helper.mjs"],
  },
});
const out = path.join(dir, "out");
const { sourceFile, specFile, providerFile } = files;
const manifest = writeDeployment(sourceFile, specFile, out);
assertPublicEmissionRefused(manifest, out);
await withSplitPreview({ manifest }, async ({ directory, entryFile }) => {
  copyFileSync(providerFile, path.join(directory, "provider.mjs"));
  for (const file of [sourceFile, entryFile]) assert.equal((await import(pathToFileURL(file).href)).answer, 512);
  const model = probeLinkedModel({
    source: sourceFile,
    spec: specFile,
    directory,
    providers: ["provider.mjs"],
    observe: [["answer"]],
  });
  assert.equal(model.status, "matched", JSON.stringify(model));
  assert(model.fuel > 0);
  assert.deepEqual(model.observed, expectedModelObservations([[["answer"], 512]]));
  assertPublicEmissionRefused(manifest, directory);
  assertPublicRecheckRefused(manifest, directory);
});
