import { computeJsAsts } from "../common/parse_asts.mjs";
import { loadJsChunks } from "../common/load_chunks.mjs";
import { normalizeJsChunks } from "../split/function_parts.mjs";

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
