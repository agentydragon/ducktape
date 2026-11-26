import assert from "node:assert/strict";
import path from "node:path";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { observe, prepare, root } from "./harness.mjs";

export function prepareLinkedFixture(name, source, spec) {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider: "", spec });
  const out = path.join(dir, "out");
  const manifest = writeDeployment(files.sourceFile, files.specFile, out);
  return { name, ...files, out, manifest };
}

export function prepareWorkerFixture(name, source, entryUnits = ["#1"]) {
  return prepareLinkedFixture(name, source, {
    entry: "entry.mjs",
    modules: { "worker.mjs": ["#0"], "entry.mjs": entryUnits },
    order: ["worker.mjs"],
  });
}

function modelText(value) {
  if (typeof value === "number") {
    const bits = Buffer.alloc(8);
    bits.writeDoubleBE(value);
    return `#${bits.readBigUInt64BE()}`;
  }
  if (value === undefined) return "undefined";
  assert(value === null || typeof value === "boolean" || typeof value === "string", "sample must be primitive");
  return JSON.stringify(value);
}

/** Check concrete initialization with the test-only CoreJS probe at its chosen fuel. */
export function assertMatchedModel({ name, sourceFile, specFile }, directory, samples) {
  const model = probeLinkedModel({
    source: sourceFile,
    spec: specFile,
    directory,
    observe: samples.map(([path]) => path),
  });
  const { fuel, ...observation } = model;
  assert.deepEqual(
    observation,
    {
      status: "matched",
      assumptions: [],
      host_refs: [],
      observed: samples.map(([path, value]) => {
        const expected = modelText(value);
        return { path, original: expected, split: expected };
      }),
    },
    name
  );
  assert(Number.isInteger(fuel) && fuel > 0, `${name}: invalid probe fuel ${fuel}`);
}

/** Compare Node's result for the original and the isolated preview. */
export async function assertLinkedPreviewResult(fixture, expected, samples = [[["result"], expected]]) {
  const { name, manifest, sourceFile } = fixture;
  assert.deepEqual(observe(sourceFile, "result"), { result: expected }, name);
  await withSplitPreview({ manifest }, ({ directory, entryFile }) => {
    assert.deepEqual(observe(entryFile, "result"), { result: expected }, name);
    assertMatchedModel(fixture, directory, samples);
  });
}
