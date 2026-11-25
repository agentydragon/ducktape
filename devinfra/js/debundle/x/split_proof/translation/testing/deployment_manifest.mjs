import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

/** Write a deployment manifest for a fixture's one changed chunk. */
export function writeDeployment(source, spec, out) {
  const plan = JSON.parse(readFileSync(spec, "utf8"));
  const resolved = Object.hasOwn(plan, "chunk_id");
  const entry = resolved ? path.basename(source) : plan.entry;
  const chunk = {
    path: entry,
    source: path.resolve(source),
    ...(resolved ? { placement: plan } : { spec: path.resolve(spec) }),
  };
  const manifest = path.resolve(out) + ".deployment.json";
  writeFileSync(manifest, JSON.stringify({ entry, chunks: [chunk] }));
  return manifest;
}
