import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
import path from "node:path";
import { writeDeployment } from "./deployment_manifest.mjs";

/**
 * Run the test-only renderer. The preview files exist only during the callback.
 * An expected exit of 1 selects a diagnostic plan refusal that still emits a preview.
 */
export async function withSplitPreview({ source, spec, manifest, expectedExit = 0 }, check) {
  assert(
    manifest ? source === undefined && spec === undefined : source && spec,
    "provide either a source and spec or a deployment manifest"
  );
  assert([0, 1].includes(expectedExit), "expectedExit must be 0 or 1");
  assert(process.env.TEST_TMPDIR, "TEST_TMPDIR is required");
  assert(process.env.SPLIT_PREVIEW, "SPLIT_PREVIEW is required");
  const work = mkdtempSync(path.resolve(process.env.TEST_TMPDIR, "split-preview-"));
  try {
    const directory = path.join(work, "modules");
    const deployment = manifest ? path.resolve(manifest) : writeDeployment(source, spec, directory);
    const result = spawnSync(process.env.SPLIT_PREVIEW, [deployment, directory, "--force"], { encoding: "utf8" });
    if (result.error) throw result.error;
    const log = result.stdout + result.stderr;
    assert.equal(result.status, expectedExit, log);
    if (expectedExit === 1) assert(result.stderr.split("\n").includes("rejected; written anyway (--force)"), log);
    const { entry } = JSON.parse(readFileSync(deployment, "utf8"));
    const entryFile = path.join(directory, entry);
    assert(statSync(entryFile).isFile(), `preview entry is not a file: ${entryFile}`);
    return await check({ directory, entryFile, log });
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}
