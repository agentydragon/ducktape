import { readFileSync } from "node:fs";
import { parse as parseJsonc } from "jsonc-parser";
import {
  extractRuntimeBoundaryMetadata,
} from "../analysis/runtime_boundary_metadata_lib.mjs";
import {
  extractScrambledIdentifierFrequencies,
} from "../analysis/scrambled_identifier_frequency_lib.mjs";
import { computeJsAsts } from "../common/compute_js_asts_lib.mjs";
import { createEmptyArtifact } from "../common/pipeline_artifact_lib.mjs";
import { loadJsChunks } from "../common/load_js_chunks_lib.mjs";
import { extractAtomicModules } from "../extract/atomic_modules_stage_lib.mjs";
import { extractOrderedInitRegions } from "../extract/extract_ordered_init_regions_lib.mjs";
import { extractGuidedSelectedOwnerModules } from "../extract/packed_selected_modules_stage_lib.mjs";
import { mergeModules } from "../extract/merge_modules_stage_lib.mjs";
import { emitBrowserHarness } from "../harness/emit_browser_harness_lib.mjs";
import { renameBindingsInArtifact } from "../rename/rename_bindings_lib.mjs";
import { normalizeJsChunks, splitFunctionParts } from "../split/split_function_parts_lib.mjs";
import { applyVendorAnnotations } from "../vendor/apply_vendor_annotations_lib.mjs";
import { renameVendorExports } from "../vendor/rename_vendor_exports_lib.mjs";
import { swapVendorChunks } from "../vendor/swap_vendor_chunks_lib.mjs";
import { formatDuration, logProgress, relativeWorkspacePath, resolveWorkspacePath } from "../common/workspace_io_lib.mjs";
import { rewriteChunkEntrySpecifiers } from "./rewrite_chunk_entry_specifiers_lib.mjs";
import { writeJsTree } from "./write_js_tree_lib.mjs";

const STAGE_HANDLERS = Object.freeze({
  load_js_chunks: loadJsChunks,
  compute_js_asts: computeJsAsts,
  normalize_js_chunks: normalizeJsChunks,
  split_function_parts: splitFunctionParts,
  apply_vendor_annotations: applyVendorAnnotations,
  rename_vendor_exports: renameVendorExports,
  rename_bindings: renameBindingsInArtifact,
  rewrite_chunk_entry_specifiers: rewriteChunkEntrySpecifiers,
  extract_runtime_boundary_metadata: extractRuntimeBoundaryMetadata,
  swap_vendor_chunks: swapVendorChunks,
  extract_scrambled_identifier_frequencies: extractScrambledIdentifierFrequencies,
  emit_browser_harness: emitBrowserHarness,
  extract_ordered_init_regions: extractOrderedInitRegions,
  extract_atomic_modules: extractAtomicModules,
  extract_guided_selected_owner_modules: extractGuidedSelectedOwnerModules,
  merge_modules: mergeModules,
  write_js_tree: writeJsTree,
});

export async function runTransformSpec(specPath, { packageRoots, packagesRoot } = {}) {
  const absoluteSpecPath = resolveWorkspacePath(specPath);
  const spec = parseJsonWithComments(readFileSync(absoluteSpecPath, "utf8"), absoluteSpecPath);
  return runTransformSpecObject(spec, { packageRoots, packagesRoot, specPath: absoluteSpecPath });
}

export async function runTransformSpecObject(spec, { packageRoots, packagesRoot, specPath = "<object>" } = {}) {
  validateSpec(spec);
  const operations = spec.operations ?? [];
  const pipelineStartedAt = process.hrtime.bigint();

  const steps = [];
  let artifact = createEmptyArtifact();
  for (const stage of spec.pipeline) {
    if (stage.disabled === true) {
      logProgress(`stage skip id=${stage.id} operation=${stage.operation}`);
      steps.push({
        artifactMode: "skipped",
        id: stage.id,
        operation: stage.operation,
      });
      continue;
    }
    const handler = resolveStageHandler(stage);
    const args = stage.args ?? {};
    const startedAt = process.hrtime.bigint();
    logProgress(`stage start id=${stage.id} operation=${stage.operation}`);
    const result = await handler({
      artifact,
      ...(packageRoots ? { packageRoots } : {}),
      ...(packagesRoot ? { packagesRoot } : {}),
      ...args,
      operations,
    });
    if (!result?.artifact) {
      throw new Error(`Stage ${stage.id} did not return an artifact`);
    }
    artifact = result.artifact;
    const durationMs = Number(process.hrtime.bigint() - startedAt) / 1_000_000;
    logProgress(
      `stage done id=${stage.id} operation=${stage.operation} mode=pipeline duration=${formatDuration(durationMs)}`
    );
    steps.push({
      artifactMode: "pipeline",
      durationMs,
      id: stage.id,
      ...(result?.manifest?.kind ? { manifestKind: result.manifest.kind } : {}),
      operation: stage.operation,
    });
  }

  return {
    durationMs: Number(process.hrtime.bigint() - pipelineStartedAt) / 1_000_000,
    specPath: relativeWorkspacePath(specPath),
    steps,
  };
}

export function parseJsonWithComments(text, sourceName = "<jsonc>") {
  try {
    return parseJsonc(text);
  } catch (error) {
    error.message = `Failed to parse ${sourceName}: ${error.message}`;
    throw error;
  }
}

function validateSpec(spec) {
  if (spec.kind !== "js.ast_transform_spec") {
    throw new Error(`Unsupported transform spec kind: ${spec.kind}`);
  }
  if (!Array.isArray(spec.pipeline)) {
    throw new Error("Transform spec must contain a pipeline array");
  }
  if (spec.operations !== undefined && !Array.isArray(spec.operations)) {
    throw new Error("Transform spec operations must be an array when present");
  }
  const seenStageIds = new Set();
  for (const stage of spec.pipeline) {
    if (typeof stage?.id !== "string" || stage.id === "") {
      throw new Error("Pipeline stage is missing id");
    }
    if (typeof stage?.operation !== "string" || stage.operation === "") {
      throw new Error(`Pipeline stage ${stage.id} is missing operation`);
    }
    if (stage.id === stage.operation) {
      throw new Error(`Pipeline stage ${stage.id} must differ from operation ${stage.operation}`);
    }
    if (seenStageIds.has(stage.id)) {
      throw new Error(`Duplicate pipeline stage id: ${stage.id}`);
    }
    seenStageIds.add(stage.id);
    if (stage.implementation !== undefined) {
      throw new Error(
        `Pipeline stage ${stage.id} uses legacy implementation wiring; stages now dispatch by operation only`
      );
    }
  }
}

function resolveStageHandler(stage) {
  const fn = STAGE_HANDLERS[stage.operation];
  if (!fn) {
    throw new Error(`No registered stage handler for operation ${stage.operation}`);
  }
  return fn;
}
