import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";

export const VISUAL_REVIEW_SCHEMA = "ducktape.visual-review.v1";
export const VISUAL_REVIEW_MANIFEST = "visual-review.json";

export function writeVisualReviewManifest(outputDir, { title, assets }) {
  if (!title?.trim()) throw new Error("visual-review title must not be empty");
  if (!assets?.length) throw new Error("visual review must contain at least one asset");
  const paths = assets.map(({ path }) => path);
  if (paths.some((path) => basename(path) !== path || !path.endsWith(".png"))) {
    throw new Error("visual-review assets must be safe PNG basenames");
  }
  if (new Set(paths).size !== paths.length) throw new Error("visual-review asset paths must be unique");

  mkdirSync(outputDir, { recursive: true });
  const destination = join(outputDir, VISUAL_REVIEW_MANIFEST);
  writeFileSync(destination, `${JSON.stringify({ schema: VISUAL_REVIEW_SCHEMA, title, assets }, null, 2)}\n`);
  return destination;
}

// Add one asset to the manifest, creating it if absent. Lets a runner retain
// renders one at a time without knowing the full asset list upfront;
// re-adding an existing path is a no-op.
export function upsertVisualReviewAsset(outputDir, { title, asset }) {
  const manifestPath = join(outputDir, VISUAL_REVIEW_MANIFEST);
  const assets = existsSync(manifestPath) ? (JSON.parse(readFileSync(manifestPath, "utf8")).assets ?? []) : [];
  if (!assets.some(({ path }) => path === asset.path)) assets.push(asset);
  return writeVisualReviewManifest(outputDir, { title, assets });
}
