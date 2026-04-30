import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { createTempFixtureRoot } from "../test_support/fixtures.mjs";
import { buildExcalidrawReferenceGolden } from "./generate_excalidraw_reference.mjs";

test("excalidraw reference output matches committed golden", async () => {
  const root = createTempFixtureRoot("debundle-excalidraw-reference-golden-");
  const actualRoot = join(root, "actual");

  await buildExcalidrawReferenceGolden(actualRoot);

  const expectedRoot = fileURLToPath(new URL("./testdata/excalidraw_reference/", import.meta.url));
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
    // Sibling notes (e.g. HUMANLIKE_GAPS.md) live alongside the golden but
    // are not part of the generator output.
    if (entry.name.endsWith(".md")) continue;
    const relativePath = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) {
      files.push(...listFiles(root, relativePath));
    } else {
      files.push(relativePath);
    }
  }
  return files.sort();
}
