import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { buildJsGolden, buildRustGolden, computeDiffSummary, listFiles } from "./pipeline_impl_golden_lib.mjs";

const GOLDEN_ROOT = fileURLToPath(new URL("./testdata/mock_browser_bundle/pipeline_impl_golden/", import.meta.url));
const JS_GOLDEN_ROOT = fileURLToPath(new URL("./testdata/mock_browser_bundle/pipeline_golden/app/", import.meta.url));

test("js and rust pipeline outputs match committed goldens and tracked diff", async () => {
  const tmp = mkdtempSync(join(tmpdir(), "debundle-impl-golden-test-"));
  const jsActual = join(tmp, "js");
  const rustActual = join(tmp, "rust");

  await buildJsGolden(jsActual);
  buildRustGolden(rustActual, resolveRustBin());

  assertTreeEquals(jsActual, JS_GOLDEN_ROOT);
  assertTreeEquals(rustActual, join(GOLDEN_ROOT, "rust"));

  const actualDiff = computeDiffSummary(jsActual, rustActual);
  const expectedDiff = readFileSync(join(GOLDEN_ROOT, "js_rust_diff.txt"), "utf8");
  assert.equal(actualDiff, expectedDiff);
});

function resolveRustBin() {
  const runfiles = process.env.TEST_SRCDIR;
  const workspace = process.env.TEST_WORKSPACE;
  const rel = process.env.RUST_DEBUNDLE_BIN;
  assert.ok(runfiles && workspace && rel, "bazel runfiles env for rust bin missing");
  return join(runfiles, workspace, rel);
}

function assertTreeEquals(actualRoot, expectedRoot) {
  const actualFiles = listFiles(actualRoot).filter((f) => !isTransientPath(f));
  const expectedFiles = listFiles(expectedRoot).filter((f) => !isTransientPath(f));
  assert.deepEqual(actualFiles, expectedFiles);
  for (const rel of expectedFiles) {
    assert.equal(
      normalizeContent(readFileSync(join(actualRoot, rel), "utf8"), rel),
      normalizeContent(readFileSync(join(expectedRoot, rel), "utf8"), rel),
      rel
    );
  }
}

function isTransientPath(path) {
  return /^static\/[^/]+\/(entry\.js|manifest\.json)$/.test(path) || path === "planner_snapshot.json" || path === "analysis_snapshot.json";
}

function normalizeContent(text, rel) {
  if (rel.endsWith("manifest.json") && text.includes("/tmp/debundle-impl-golden-js-")) {
    const parsed = JSON.parse(text);
    const outDir = parsed.outDir;
    if (typeof outDir === "string" && outDir.includes("/app")) {
      const root = outDir.slice(0, outDir.lastIndexOf("/app"));
      return JSON.stringify(rewritePaths(parsed, root), null, 2) + "\n";
    }
  }
  if (rel === "manifest.json") {
    try {
      const parsed = JSON.parse(text);
      if (parsed?.schemaVersion === 1 && parsed?.scriptSource === "split") {
        parsed.sourceHtml = "__PIPELINE_ROOT__/snapshot/index.html";
        parsed.snapshotRoot = "__PIPELINE_ROOT__/snapshot";
        parsed.assetSummaryPath = "__PIPELINE_ROOT__/extracted/asset-summary.json";
        parsed.chunksManifestPath = "__PIPELINE_ROOT__/app/chunks.manifest.json";
        parsed.runtimeRoot = "__PIPELINE_ROOT__/app";
        parsed.outDir = "__PIPELINE_ROOT__/app";
        if (parsed.generated) {
          parsed.generated.bootstrap = "__PIPELINE_ROOT__/app/bootstrap.js";
          parsed.generated.chunksManifest = "__PIPELINE_ROOT__/app/chunks.manifest.json";
          parsed.generated.indexHtml = "__PIPELINE_ROOT__/app/index.html";
        }
        return JSON.stringify(parsed, null, 2) + "\n";
      }
    } catch {
      // non-json manifest content; leave as-is
    }
  }
  return text;
}

function rewritePaths(value, prefix) {
  if (typeof value === "string") {
    return value.replaceAll(prefix, "__PIPELINE_ROOT__");
  }
  if (Array.isArray(value)) {
    return value.map((v) => rewritePaths(v, prefix));
  }
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      out[k] = rewritePaths(v, prefix);
    }
    return out;
  }
  return value;
}
