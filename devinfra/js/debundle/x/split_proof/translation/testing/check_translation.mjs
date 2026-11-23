import { spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { frontTree } from "../frontend.mjs";
import { pairFiles } from "../pair.mjs";

// Reparse the emitted modules independently and give Lean their pairing with the source.
export function checkTranslation(bundle, dir) {
  const bundleFile = dir + "-bundle.json";
  const treeFile = dir + "-tree.json";
  const certFile = dir + "-cert.json";
  writeFileSync(bundleFile, JSON.stringify(bundle));
  writeFileSync(treeFile, JSON.stringify(frontTree(dir, "entry.mjs")));
  pairFiles(bundleFile, treeFile, certFile);
  return spawnSync(process.env.TRANSLATE, [bundleFile, treeFile, certFile], { encoding: "utf8" });
}
