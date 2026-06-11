// Test fixture helpers for live_proxy tests. Lifted from the deleted
// `test_support/fixtures.mjs` to keep the live_proxy tests self-contained.

import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export function createWebFixtureRoots(prefix) {
  const root = mkdtempSync(join(tmpdir(), prefix));
  return {
    analysisRoot: join(root, "analysis"),
    appRoot: join(root, "app"),
    extractedRoot: join(root, "extracted"),
    outRoot: join(root, "out"),
    packagesRoot: join(root, "node_modules"),
    root,
    snapshotRoot: join(root, "snapshot"),
    sourceRoot: join(root, "source"),
    splitRoot: join(root, "split"),
    transformedRoot: join(root, "transformed"),
    vendorsRoot: join(root, "vendors"),
  };
}

export function readUtf8(path) {
  return readFileSync(path, "utf8");
}
