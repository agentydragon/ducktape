import { cpSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";

import { runMockBrowserBundlePipeline } from "./mock_pipeline.mjs";

const PIPELINE_ROOT_PLACEHOLDER = "__PIPELINE_ROOT__";

export async function buildMockBrowserPipelineGolden(outRoot) {
  const root = resolve(outRoot);
  rmSync(root, { force: true, recursive: true });
  mkdirSync(root, { recursive: true });

  const { appRoot, root: pipelineRoot, transformedRoot } = await runMockBrowserBundlePipeline({
    prefix: "debundle-browser-pipeline-golden-",
  });
  cpSync(appRoot, join(root, "app"), { recursive: true });
  cpSync(transformedRoot, join(root, "transformed"), { recursive: true });
  rewritePipelineRootReferences(root, pipelineRoot);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const outRoot = process.argv[2];
  if (!outRoot) {
    throw new Error("usage: build_mock_golden.mjs <out-root>");
  }
  await buildMockBrowserPipelineGolden(outRoot);
}

function rewritePipelineRootReferences(root, pipelineRoot) {
  for (const entry of readdirSync(root)) {
    rewriteEntry(join(root, entry), pipelineRoot);
  }
}

function rewriteEntry(path, pipelineRoot) {
  if (statSync(path).isDirectory()) {
    for (const entry of readdirSync(path)) {
      rewriteEntry(join(path, entry), pipelineRoot);
    }
    return;
  }
  const original = readFileSync(path, "utf8");
  const rewritten = original.replaceAll(pipelineRoot, PIPELINE_ROOT_PLACEHOLDER);
  if (rewritten !== original) {
    writeFileSync(path, rewritten);
  }
}
