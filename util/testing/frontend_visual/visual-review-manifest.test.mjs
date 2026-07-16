import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { upsertVisualReviewAsset, writeVisualReviewManifest } from "./visual-review-manifest.mjs";

const outputDir = mkdtempSync(join(tmpdir(), "visual-review-"));
const destination = writeVisualReviewManifest(outputDir, {
  title: "Example UI",
  assets: [{ path: "screen.png", label: "Screen" }],
});

assert.deepEqual(JSON.parse(readFileSync(destination, "utf8")), {
  schema: "ducktape.visual-review.v1",
  title: "Example UI",
  assets: [{ path: "screen.png", label: "Screen" }],
});
assert.throws(
  () =>
    writeVisualReviewManifest(outputDir, {
      title: "Example UI",
      assets: [{ path: "../screen.png", label: "Screen" }],
    }),
  /safe PNG basenames/
);

const upsertDir = mkdtempSync(join(tmpdir(), "visual-review-upsert-"));
upsertVisualReviewAsset(upsertDir, { title: "Example UI", asset: { path: "a.png", label: "A" } });
upsertVisualReviewAsset(upsertDir, { title: "Example UI", asset: { path: "b.png", label: "B" } });
// Re-adding an existing path is a no-op.
const upserted = upsertVisualReviewAsset(upsertDir, { title: "Example UI", asset: { path: "a.png", label: "A2" } });
assert.deepEqual(JSON.parse(readFileSync(upserted, "utf8")), {
  schema: "ducktape.visual-review.v1",
  title: "Example UI",
  assets: [
    { path: "a.png", label: "A" },
    { path: "b.png", label: "B" },
  ],
});
