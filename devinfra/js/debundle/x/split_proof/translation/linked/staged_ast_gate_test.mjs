import assert from "node:assert/strict";
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";

const root = path.join(process.env.TEST_TMPDIR, "staged-ast-gate");
const source = path.join(root, "source.mjs");
const spec = path.join(root, "spec.json");
const provider = path.join(root, "provider.mjs");
const out = path.join(root, "out");
mkdirSync(root, { recursive: true });
const payload = "x".repeat(220_000);
writeFileSync(provider, "export {};\n");
writeFileSync(
  source,
  `import "./provider.mjs";\nlet count = 0;\nconst hostType = typeof window;\nconst payload = ${JSON.stringify(payload)};\nexport { count, hostType, payload };\n`
);
writeFileSync(spec, JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#1", "#2", "#3", "#4"] } }));
// The public command and test-only workers run with unrelated ambient Lean paths.
process.env.LEAN_PATH = path.join(root, "untrusted-imports");
process.env.LEAN_SRC_PATH = path.join(root, "untrusted-sources");
process.env.LEAN_SYSROOT = path.join(root, "untrusted-sdk");
const manifest = writeDeployment(source, spec, out);
assertPublicEmissionRefused(manifest, out);

await withSplitPreview({ manifest }, async ({ directory, entryFile, log }) => {
  copyFileSync(provider, path.join(directory, "provider.mjs"));
  assert.match(log, /render \+ write/);
  for (const file of [source, entryFile]) {
    const original = await import(pathToFileURL(file).href);
    assert.equal(original.count, 0);
    assert.equal(original.hostType, "undefined");
    assert.equal(original.payload, payload);
  }

  const options = {
    source,
    spec,
    directory,
    providers: ["provider.mjs"],
    host: { profile: "browser-page" },
    observe: [["count"], ["hostType"]],
  };
  const model = probeLinkedModel(options);
  assert.equal(model.status, "matched", JSON.stringify(model));
  assert.deepEqual(model.observed, [
    { path: ["count"], original: "#0", split: "#0" },
    { path: ["hostType"], original: '"object"', split: '"object"' },
  ]);
  assert.deepEqual(model.assumptions, ["typeof window is object without observable effects throughout initialization"]);

  const before = readFileSync(entryFile, "utf8");
  const tampered = before.replace("count = 0", "count = 1");
  assert.notEqual(tampered, before);
  writeFileSync(entryFile, tampered);
  const mutated = probeLinkedModel(options);
  assert.equal(mutated.status, "refused", JSON.stringify(mutated));
  assert.equal(mutated.reason_code, "linked_input", JSON.stringify(mutated));
  assert.match(mutated.reason, /emitted syntax differs from original unit 1/);
  assertPublicEmissionRefused(manifest, directory);
  assertPublicRecheckRefused(manifest, directory);
});
