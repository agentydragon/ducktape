import { join, posix } from "node:path";
import {
  createFile,
  getArtifactChunkManifest,
  getArtifactManifestOrDerived,
  getChunkEntryPath,
  getChunkFile,
  requirePipelineArtifact,
  setArtifactChunkManifest,
  setArtifactManifest,
  setFile,
} from "../common/artifact.mjs";
import {
  formatDuration,
  formatDurationSince,
  logProgress,
  relativeWorkspacePath,
  resolveWorkspacePath,
} from "../common/io.mjs";
import { cloneDefaultParserOptions } from "../common/parser_options.mjs";
import { extractOrderedInitRegionsInAst } from "./init_region.mjs";
import { writeJsTree } from "../transforms/write.mjs";

const EXTRACT_OPERATION_TYPES = new Set([
  "extract_ordered_init_region",
  "extract_ordered_init_owner_closure_pass",
]);

export function extractOrderedInitRegions(options) {
  const artifact = requirePipelineArtifact(options.artifact, "extractOrderedInitRegions");
  const outDir = resolveWorkspacePath(options.outDir);
  const shouldWrite = options.write ?? true;
  const operations = options.operations ?? [];
  const extractOperations = operations.filter((operation) => EXTRACT_OPERATION_TYPES.has(operation.operation));
  const groupedByChunkId = groupOperationsByChunkAndTarget(artifact, extractOperations);
  logProgress(
    `extract-ordered-init start operations=${extractOperations.length} mode=pipeline out=${relativeWorkspacePath(outDir)}`
  );
  const startedAt = process.hrtime.bigint();
  const { applied, targetTimings } = applyExtractionsToArtifact(artifact, groupedByChunkId);

  const outputManifest = buildOutputManifest({
    applied,
    manifest: getArtifactManifestOrDerived(artifact),
    outDir,
  });
  setArtifactManifest(artifact, outputManifest);
  if (shouldWrite) {
    writeJsTree({ artifact, force: options.force ?? false, outDir });
  }
  logExtractDone({ applied, copyMs: 0, startedAt, targetTimings });

  return {
    artifact,
    manifest: outputManifest,
  };
}

function applyExtractionsToArtifact(artifact, groupedByChunkId) {
  const applied = [];
  const targetTimings = [];
  for (const [chunkId, chunkTargets] of groupedByChunkId) {
    const chunkManifest = requireChunkManifest(artifact, chunkId);
    const entryFile = resolveChunkEntryFile(artifact, chunkId, chunkManifest);
    const chunkApplied = [];
    for (const [targetRelativePath, targetOperations] of chunkTargets) {
      const targetStartedAt = process.hrtime.bigint();
      const fileArtifact = getChunkFile(artifact, chunkId, targetRelativePath);
      if (!fileArtifact?.ast) {
        throw new Error(`Ordered-init extraction targets missing in-memory file: ${targetRelativePath}`);
      }
      const file = normalizeRelativeFile(fileArtifact.metadata?.chunkFile ?? fileArtifact.path);
      for (const operation of targetOperations) {
        if (operation.operation === "extract_ordered_init_owner_closure_pass") {
          continue;
        }
        const targetFile = normalizeRelativeFile(operation.target.file);
        if (targetFile !== file && getChunkFile(artifact, chunkId, targetFile)) {
          throw new Error(
            `Extract operation ${operation.id} target file already exists in chunk ${chunkId}: ${targetFile}`
          );
        }
      }

      const result = extractOrderedInitRegionsInAst(fileArtifact.ast, targetOperations, {
        chunkId,
        file,
        headerLines: fileArtifact.headerLines ?? [],
      });
      for (const [relativePath, generatedFile] of result.jsFiles.entries()) {
        setFile(
          artifact,
          createFile({
            path: relativePath,
            ast: generatedFile.ast,
            headerLines: generatedFile.headerLines,
            parserOptions: fileArtifact.parserOptions ?? chunkManifest.parser ?? cloneDefaultParserOptions(),
            metadata: {
              ...fileArtifact.metadata,
              chunkFile: relativePath,
              chunkId,
              role: relativePath === entryFile ? "entry" : "module",
            },
          })
        );
      }

      applied.push(...result.applied);
      chunkApplied.push(...result.applied);
      targetTimings.push({
        operations: targetOperations.length,
        targetRelativePath,
        durationMs: Number(process.hrtime.bigint() - targetStartedAt) / 1_000_000,
      });
    }
    if (chunkApplied.length > 0) {
      setArtifactChunkManifest(artifact, chunkId, {
        ...chunkManifest,
        orderedInitExtractions: mergeRecordsById(chunkManifest.orderedInitExtractions, chunkApplied),
      });
    }
  }

  return { applied, targetTimings };
}

function buildOutputManifest({ applied, manifest, outDir }) {
  const mergedOrderedInitExtractions = mergeRecordsById(manifest.orderedInitExtractions, applied);
  const { sourceInputRoot: existingSourceInputRoot, ...manifestWithoutSourceRoot } = manifest;
  const sourceInputRoot = Object.hasOwn(manifest, "sourceInputRoot") ? existingSourceInputRoot : null;
  return {
    ...manifestWithoutSourceRoot,
    outDir: relativeWorkspacePath(outDir),
    sourceInputRoot,
    counts: {
      ...manifest.counts,
      orderedInitExtractions: mergedOrderedInitExtractions.length,
    },
    chunks: manifest.chunks.map((chunk) => ({
      ...chunk,
      outputPath: relativeWorkspacePath(join(outDir, ...chunk.chunkId.split("/"))),
    })),
    orderedInitExtractions: mergedOrderedInitExtractions,
  };
}

function logExtractDone({ applied, copyMs, startedAt, targetTimings }) {
  logProgress(
    `extract-ordered-init done applied=${applied.length} copy=${formatDuration(copyMs)} duration=${formatDurationSince(startedAt)}`
  );
  for (const target of [...targetTimings].sort((left, right) => right.durationMs - left.durationMs).slice(0, 8)) {
    logProgress(
      `extract-ordered-init slow-target target=${target.targetRelativePath} operations=${target.operations} duration=${formatDuration(
        target.durationMs
      )}`
    );
  }
}

function groupOperationsByChunkAndTarget(artifact, operations) {
  const groupedByChunkId = new Map();
  for (const operation of operations) {
    validateExtractOperationShape(operation);
    const chunkId = normalizeChunkId(operation.selector.chunkId);
    const relativePath = resolveOperationTargetFile(artifact, operation, chunkId);
    if (!groupedByChunkId.has(chunkId)) {
      groupedByChunkId.set(chunkId, new Map());
    }
    const chunkTargets = groupedByChunkId.get(chunkId);
    if (!chunkTargets.has(relativePath)) {
      chunkTargets.set(relativePath, []);
    }
    chunkTargets.get(relativePath).push(operation);
  }
  return groupedByChunkId;
}

function resolveOperationTargetFile(artifact, operation, chunkId = operation.selector.chunkId) {
  if (operation.selector.file) {
    return normalizeRelativeFile(operation.selector.file);
  }
  const entryFile = getChunkEntryPath(artifact, chunkId);
  if (entryFile) {
    return normalizeRelativeFile(entryFile);
  }
  throw new Error(`Extract operation ${operation.id} is missing selector.file and chunk entryFile`);
}

function mergeRecordsById(existing = [], appended = []) {
  const merged = new Map();
  for (const record of [...existing, ...appended]) {
    merged.set(record.id, record);
  }
  return [...merged.values()];
}

function validateExtractOperationShape(operation) {
  if (!operation?.id) {
    throw new Error("Extract operation is missing id");
  }
  if (!operation.selector?.chunkId) {
    throw new Error(`Extract operation ${operation.id} is missing selector.chunkId`);
  }
  if (operation.operation === "extract_ordered_init_owner_closure_pass") {
    if (!operation.target?.dir) {
      throw new Error(`Extract operation ${operation.id} is missing target.dir`);
    }
    return;
  }
  if (!operation.target?.file) {
    throw new Error(`Extract operation ${operation.id} is missing target.file`);
  }
}

function normalizeChunkId(value) {
  const normalized = normalizeRelativeFile(value);
  if (normalized.endsWith(".js")) {
    throw new Error(`Extract selector chunkId should not include .js: ${value}`);
  }
  return normalized;
}

function normalizeRelativeFile(value) {
  if (typeof value !== "string" || value === "") {
    throw new Error(`Expected a non-empty relative path, got: ${value}`);
  }
  const normalized = posix.normalize(value.split("\\").join("/"));
  if (normalized === "." || normalized.startsWith("/") || normalized.split("/").includes("..")) {
    throw new Error(`Invalid relative path: ${value}`);
  }
  return normalized;
}


function requireChunkManifest(artifact, chunkId) {
  const manifest = getArtifactChunkManifest(artifact, chunkId);
  if (!manifest) {
    throw new Error(`Ordered-init extraction requires a chunk manifest for ${chunkId}`);
  }
  return manifest;
}

function resolveChunkEntryFile(artifact, chunkId, chunkManifest) {
  if (chunkManifest?.entryFile) {
    return normalizeRelativeFile(chunkManifest.entryFile);
  }
  const derivedEntryFile = getChunkEntryPath(artifact, chunkId);
  if (derivedEntryFile) {
    return normalizeRelativeFile(derivedEntryFile);
  }
  throw new Error(`Ordered-init extraction could not determine entry file for chunk: ${chunkId}`);
}
