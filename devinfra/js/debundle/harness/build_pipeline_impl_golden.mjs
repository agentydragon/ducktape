import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { buildJsGolden, buildRustGolden, computeDiffSummary } from "./pipeline_impl_golden_lib.mjs";

async function main() {
  const outRoot = process.argv[2];
  const rustBin = process.argv[3];
  if (!outRoot || !rustBin) {
    throw new Error("usage: build_pipeline_impl_golden.mjs <out-root> <rust-bin>");
  }
  const root = resolve(outRoot);
  rmSync(root, { recursive: true, force: true });
  mkdirSync(join(root, "js"), { recursive: true });
  mkdirSync(join(root, "rust"), { recursive: true });
  await buildJsGolden(join(root, "js"));
  buildRustGolden(join(root, "rust"), resolve(rustBin));
  const diff = computeDiffSummary(join(root, "js"), join(root, "rust"));
  writeFileSync(join(root, "js_rust_diff.txt"), diff);
}

await main();
