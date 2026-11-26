import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { dump } from "../parser.mjs";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { observe, root, threeUnitFixture as fixture } from "./harness.mjs";

// Execute a concrete recorded-world model, not an equivalence certificate.
function modelRun(file, providers = {}, observations = {}) {
  assert(process.env.COREJS, "COREJS is required");
  const readAst = (name) => dump(readFileSync(name, "utf8"), { includeOpaqueCaptures: true });
  const input = {
    ...readAst(file),
    entry: path.basename(file),
    providers: Object.fromEntries(Object.entries(providers).map(([name, value]) => [name, readAst(value)])),
    ...observations,
  };
  const inputFile = path.join(root, "symbol-tag-model.jsonl");
  writeFileSync(inputFile, JSON.stringify(input) + "\n");
  const run = spawnSync(process.env.COREJS, ["--ast", inputFile], { encoding: "utf8" });
  if (run.error) throw run.error;
  assert.equal(run.status, 0, run.stderr);
  return JSON.parse(run.stdout.trim());
}

const tag = fixture(
  "class-string-tag",
  `class Box {
    get [Symbol.toStringTag]() { return this.label; }
    [Symbol.iterator]() { return 17; }
  }
  const result = (() => {
    const box = new Box();
    box.label = "Box";
    const key = Symbol.toStringTag;
    if (typeof key !== "symbol" || key !== Symbol.toStringTag || key === Symbol.iterator) throw 1;
    if (box[key] !== "Box" || box[Symbol.iterator]() !== 17 || box["Symbol.toStringTag"] !== undefined) throw 2;
    return true;
  })();
  export { result };`
);

// The public command refuses without producing output. A model preview below is
// a separate, test-only operation and does not certify the placement.
const manifest = writeDeployment(tag.sourceFile, tag.specFile, tag.out);
assertPublicEmissionRefused(manifest, tag.out);
assert(!existsSync(tag.out), "public refusal created output");

await withSplitPreview({ source: tag.sourceFile, spec: tag.specFile }, async ({ directory, entryFile }) => {
  assert.deepEqual(observe(tag.sourceFile, "result"), { result: true });
  assert.deepEqual(observe(entryFile, "result"), { result: true });

  const observations = { toStringTagSymbol: true, iteratorSymbol: true };
  const providers = { "worker.mjs": path.join(directory, "worker.mjs") };
  for (const [file, modules] of [
    [tag.sourceFile, {}],
    [entryFile, providers],
  ]) {
    const result = modelRun(file, modules, observations);
    assert.equal(result.end, "normal", JSON.stringify(result));
    assert.deepEqual(result.out, []);
    assert.equal(result.world, "");
    for (const [omitted, name] of [
      ["toStringTagSymbol", "Symbol.toStringTag"],
      ["iteratorSymbol", "Symbol.iterator"],
    ]) {
      const missing = modelRun(file, modules, { ...observations, [omitted]: false });
      assert.equal(missing.end, "unknown", JSON.stringify(missing));
      assert.equal(missing.detail, `unsupported host reference ${name}`);
    }
  }
  // This pair probe reports concrete observations, not an all-world proof.
  const probeOptions = {
    source: tag.sourceFile,
    spec: tag.specFile,
    directory,
    host: { profile: "browser-page" },
    observe: [["result"]],
  };
  const pair = probeLinkedModel(probeOptions);
  assert.equal(pair.status, "matched", JSON.stringify(pair));
  assert(Number.isInteger(pair.fuel) && pair.fuel > 0, JSON.stringify(pair));
  assert.deepEqual(pair.observed, [{ path: ["result"], original: "true", split: "true" }]);
  assert.deepEqual(
    new Set(pair.assumptions),
    new Set([
      "Symbol.toStringTag returns the well-known toStringTag symbol without observable effects throughout initialization",
      "Symbol.iterator returns the well-known iterator symbol without observable effects throughout initialization",
    ])
  );
  assert.deepEqual(
    new Set(pair.host_refs.map(({ receiver, key, identity }) => `${receiver}.${key}=${identity}`)),
    new Set(["Symbol.toStringTag=Symbol.toStringTag", "Symbol.iterator=Symbol.iterator"])
  );
  const empty = probeLinkedModel({ ...probeOptions, host: undefined });
  assert.equal(empty.status, "refused", JSON.stringify(empty));
  assert.equal(empty.reason_code, "linked_input");
  assert.match(empty.reason, /unsupported computed class key/);

  const worker = path.join(directory, "worker.mjs");
  const source = readFileSync(worker, "utf8");
  const changed = source.replace(/\[\s*Symbol\.toStringTag\s*\]/, '["Symbol.toStringTag"]');
  assert.notEqual(source, changed);
  writeFileSync(worker, changed);
  const changedPair = probeLinkedModel(probeOptions);
  assert.equal(changedPair.status, "refused", JSON.stringify(changedPair));
  assert.equal(changedPair.reason_code, "linked_input");
  assert.match(changedPair.reason, /emitted syntax differs from original unit 0/);
  const mutant = modelRun(entryFile, providers, observations);
  assert.equal(mutant.end, "throw", JSON.stringify(mutant));
  assert.equal(mutant.detail, "number:2");
  await assert.rejects(import(pathToFileURL(entryFile).href), (error) => error === 2);

  // The public emission and retired check-output option both refuse without
  // changing even a tampered preview. Neither is an artifact equivalence check.
  const snapshot = () =>
    readdirSync(directory)
      .sort()
      .map((name) => [name, readFileSync(path.join(directory, name))]);
  const before = snapshot();
  assertPublicEmissionRefused(manifest, directory);
  assertPublicRecheckRefused(manifest, directory);
  assert.deepEqual(snapshot(), before, "public refusal changed the preview");
});
