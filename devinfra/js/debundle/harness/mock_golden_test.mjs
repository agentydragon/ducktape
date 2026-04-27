import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { buildMockBrowserPipelineGolden } from "./build_mock_golden.mjs";
import { createTempFixtureRoot } from "../test_support/fixtures.mjs";
import { fixturePath } from "./mock_pipeline.mjs";

test("mock browser bundle pipeline output matches the committed golden", async () => {
  const root = createTempFixtureRoot("debundle-browser-pipeline-golden-");
  const actualRoot = join(root, "actual");

  await buildMockBrowserPipelineGolden(actualRoot);

  const expectedRoot = fileURLToPath(fixturePath("pipeline_golden/"));
  const expectedFiles = listFiles(expectedRoot);
  const actualFiles = listFiles(actualRoot);
  assert.deepEqual(actualFiles, expectedFiles);

  for (const relativePath of expectedFiles) {
    assert.equal(
      readFileSync(join(actualRoot, relativePath), "utf8"),
      readFileSync(join(expectedRoot, relativePath), "utf8"),
      relativePath
    );
  }
});

function listFiles(root, prefix = "") {
  const directory = prefix === "" ? root : join(root, prefix);
  const entries = readdirSync(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const relativePath = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) {
      files.push(...listFiles(root, relativePath));
    } else {
      files.push(relativePath);
    }
  }
  return files.sort();
}
