import { cpSync } from "node:fs";
import { join } from "node:path";

import { createWebFixtureRoots } from "../test_support/fixtures.mjs";
import { runTransformSpecObject } from "../transforms/runner.mjs";
import { createMockBrowserBundleTransformSpec } from "./mock_spec.mjs";

const FIXTURE_ROOT = new URL("./testdata/mock_browser_bundle/", import.meta.url);

export async function runMockBrowserBundlePipeline({
  prefix = "debundle-browser-harness-pipeline-",
} = {}) {
  const roots = createWebFixtureRoots(prefix);
  copyFixtureTree(roots.snapshotRoot, "generated/snapshot");
  copyFixtureTree(roots.extractedRoot, "generated/extracted");

  const spec = createMockBrowserBundleTransformSpec({
    appRoot: roots.appRoot,
    assetSummaryPath: join(roots.extractedRoot, "asset-summary.json"),
    jsListPath: join(roots.extractedRoot, "js-files.txt"),
    snapshotRoot: roots.snapshotRoot,
    transformedRoot: roots.transformedRoot,
  });

  const result = await runTransformSpecObject(spec);
  return {
    ...roots,
    result,
    spec,
  };
}

export function fixturePath(relativePath) {
  return new URL(relativePath, FIXTURE_ROOT);
}

function copyFixtureTree(destinationRoot, relativeSource) {
  cpSync(fixturePath(relativeSource), destinationRoot, { recursive: true });
}
