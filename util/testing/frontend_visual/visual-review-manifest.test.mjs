import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { writeVisualReviewManifest } from "./visual-review-manifest.mjs";

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
