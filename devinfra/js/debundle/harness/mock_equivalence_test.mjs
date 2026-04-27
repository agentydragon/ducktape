import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import { buildMockBrowserBundle } from "./build_mock_bundle.mjs";
import { createTempFixtureRoot } from "../test_support/fixtures.mjs";

const FIXTURE_ROOT = new URL("./testdata/mock_browser_bundle/", import.meta.url);
const EXPECTED_FILES = [
  "extracted/asset-summary.json",
  "extracted/js-files.txt",
  "snapshot/index.html",
  "snapshot/preload/app.css",
  "snapshot/static/ActivityPanel-DuckMock.js",
  "snapshot/static/SummaryChip-DuckMock.js",
  "snapshot/static/chunk-DuckMock.js",
  "snapshot/static/index-DuckMock.js",
];

test("mock browser bundle generator matches the committed fixture", async () => {
  const root = createTempFixtureRoot("debundle-browser-bundle-generator-");
  const actualRoot = join(root, "actual");

  await buildMockBrowserBundle(actualRoot);

  for (const relativePath of EXPECTED_FILES) {
    const expectedPath = fixturePath(join("generated", relativePath));
    const actualPath = join(actualRoot, relativePath);
    assert.equal(readFileSync(actualPath, "utf8"), readFileSync(expectedPath, "utf8"), relativePath);
  }

  assert.deepEqual(listFiles(actualRoot), EXPECTED_FILES);
});

function fixturePath(relativePath) {
  return new URL(relativePath, FIXTURE_ROOT);
}

function listFiles(root, prefix = "") {
  const entries = readdirSync(prefix === "" ? root : join(root, prefix), { withFileTypes: true });
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
