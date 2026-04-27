import { computeJsAsts } from "../common/compute_js_asts_lib.mjs";
import { loadJsChunks } from "../common/load_js_chunks_lib.mjs";
import { normalizeJsChunks } from "../split/split_function_parts_lib.mjs";

export async function buildNormalizedPipelineArtifactFromSnapshot({
  jobs = 1,
  jsListPath,
  snapshotRoot,
}) {
  const loaded = loadJsChunks({ inputRoot: snapshotRoot, jsListPath });
  const parsed = computeJsAsts({ artifact: loaded.artifact });
  return normalizeJsChunks({
    artifact: parsed.artifact,
    jobs,
  });
}
