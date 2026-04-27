import { availableParallelism } from "node:os";
import { posix } from "node:path";
import { Worker } from "node:worker_threads";
import {
  createEmptyArtifact,
  createFile,
  getArtifactChunkManifest,
  listChunks,
  requirePipelineArtifact,
  replaceChunks,
  setArtifactChunkManifest,
  setArtifactManifest,
} from "../common/pipeline_artifact_lib.mjs";
import { formatDuration, logProgress } from "../common/workspace_io_lib.mjs";
import {
  analyzeProgramShallow,
  buildChunkManifestFromAnalysis,
  CANONICAL_CHUNK_ENTRY_FILE,
  normalizeChunkEntryFile,
  splitScopeHoistedChunkAst,
} from "./split_chunk_lib.mjs";

export async function normalizeJsChunks({ artifact, jobs = undefined, entryFile = undefined }) {
  if (entryFile !== undefined) {
    throw new Error("normalizeJsChunks no longer accepts entryFile; normalized chunks always use entry.js");
  }
  if (jobs !== undefined && jobs !== 1) {
    // Fast normalize is metadata-only; worker IPC would dwarf the actual work.
    logProgress(`normalize ignoring jobs=${jobs}; normalize is metadata-only and runs serially`);
  }
  requirePipelineArtifact(artifact, "normalizeJsChunks");
  return rebuildJsChunks({
    artifact,
    jobs: 1,
    logLabel: "normalize",
    transformChunk: (chunk) => normalizeOneJsChunk({ artifactChunk: chunk }),
    workerDataForChunk: () => {
      throw new Error("normalizeJsChunks does not use workers");
    },
  });
}

// Conservative top-level extraction: emits one part file per SCC of named
// FunctionDeclarations whose entire transitive dep set is also extractable.
// Variables, classes, side-effecting statements, and any function depending on
// non-extractable code stay in the entry file. This is a strict subset of what
// extractAtomicModules handles; prefer that for richer extraction.
export async function splitFunctionParts({ artifact, jobs, emitParts = undefined, entryFile = undefined }) {
  if (emitParts !== undefined) {
    throw new Error("splitFunctionParts no longer accepts emitParts; it always runs the naive parts pass");
  }
  if (entryFile !== undefined) {
    throw new Error("splitFunctionParts no longer accepts entryFile; normalizeJsChunks owns entry-file selection");
  }
  requireNormalizedArtifact(artifact, "splitFunctionParts");
  return rebuildJsChunks({
    artifact,
    jobs,
    logLabel: "split-function-parts",
    transformChunk: (chunk) => splitOneJsChunkIntoFunctionParts({ artifactChunk: chunk }),
    workerDataForChunk: (chunk) => ({
      artifactChunk: chunk,
      mode: "split",
    }),
  });
}

async function rebuildJsChunks({ artifact, jobs, logLabel, transformChunk, workerDataForChunk }) {
  const sourceChunks = listChunks(artifact);
  const effectiveJobs = jobs ?? defaultJobs();

  logProgress(`${logLabel} start chunks=${sourceChunks.length} jobs=${effectiveJobs} mode=pipeline`);
  const startedAt = process.hrtime.bigint();

  const chunks =
    effectiveJobs === 1 || sourceChunks.length <= 1
      ? sourceChunks.map(transformChunk)
      : await splitChunksParallel({
          workerDataByChunk: sourceChunks.map(workerDataForChunk),
          jobs: effectiveJobs,
        });

  const nextArtifact = createEmptyArtifact();
  const manifest = buildFunctionPartsManifest(chunks);

  const outputChunks = [];
  for (const chunk of chunks) {
    setArtifactChunkManifest(nextArtifact, chunk.chunkId, chunk.manifest);
    outputChunks.push({
      chunkId: chunk.chunkId,
      entryFile: chunk.manifest.entryFile,
      files: [...chunk.jsFiles.entries()].map(([relativeFile, fileArtifact]) =>
        createFile({
          path: relativeFile,
          ast: fileArtifact.ast,
          headerLines: fileArtifact.headerLines,
          parserOptions: chunk.manifest.parser,
          metadata: {
            chunkFile: relativeFile,
            chunkId: chunk.chunkId,
            role: relativeFile === chunk.manifest.entryFile ? "entry" : "module",
            sourcePath: chunk.inputPath,
          },
        })
      ),
      metadata: {
        sourcePath: chunk.inputPath,
      },
    });
  }
  replaceChunks(nextArtifact, outputChunks);
  setArtifactManifest(nextArtifact, manifest);

  const durationMs = Number(process.hrtime.bigint() - startedAt) / 1_000_000;
  logProgress(
    `${logLabel} done chunks=${manifest.counts.chunks} parts=${manifest.counts.parts} duration=${formatDuration(
      durationMs
    )} maxChunk=${formatDuration(Math.max(...chunks.map((chunk) => chunk.timing.durationMs)))} sumChunks=${formatDuration(
      chunks.reduce((sum, chunk) => sum + chunk.timing.durationMs, 0)
    )}`
  );
  for (const chunk of slowestChunks(chunks, 8)) {
    logProgress(
      `${logLabel} slow-chunk chunk=${chunk.chunkId} duration=${formatDuration(chunk.timing.durationMs)} input=${formatBytes(
        chunk.timing.inputBytes
      )} parts=${chunk.counts.parts} keptOwners=${chunk.counts.keptTopLevelDeclarationOwners}`
    );
  }

  return {
    artifact: nextArtifact,
    manifest,
  };
}

export function normalizeOneJsChunk({ artifactChunk }) {
  // Fast path: normalize is purely metadata. Compute imports/exports/owners
  // shallowly (single ast.program.body iteration) and synthesize the chunk
  // manifest. No AST clone, no full traverse, no source-code re-emit. The
  // output chunk reuses the input AST under the canonical entry file name.
  const entryArtifactFile = artifactChunk?.files?.find((file) => file.path === artifactChunk.entryFile);
  if (!entryArtifactFile?.ast) {
    throw new Error(
      `normalizeOneJsChunk requires AST for chunk: ${artifactChunk?.chunkId ?? "<unknown>"}/${artifactChunk?.entryFile ?? "<entry>"}`
    );
  }
  const startedAt = process.hrtime.bigint();
  const jsPath = sourcePathForLoadedChunk(artifactChunk);
  const inputBytes = entryArtifactFile.content ? Buffer.byteLength(entryArtifactFile.content) : 0;
  const analysis = analyzeProgramShallow(entryArtifactFile.ast);
  const manifest = buildChunkManifestFromAnalysis(
    artifactChunk.chunkId,
    CANONICAL_CHUNK_ENTRY_FILE,
    jsPath,
    analysis,
  );
  const jsFiles = new Map([
    [
      CANONICAL_CHUNK_ENTRY_FILE,
      {
        ast: entryArtifactFile.ast,
        // Header lines kept identical to the legacy splitScopeHoistedChunkAst
        // path so consumers like write_js_tree produce byte-identical output.
        headerLines: [
          "// Generated by //devinfra/js/debundle/split:split_chunk.",
          "// Executable chunk entry.",
        ],
      },
    ],
  ]);
  return {
    chunkId: artifactChunk.chunkId,
    inputPath: jsPath,
    counts: manifest.counts,
    jsFiles,
    manifest,
    timing: {
      durationMs: Number(process.hrtime.bigint() - startedAt) / 1_000_000,
      inputBytes,
    },
  };
}

export function splitOneJsChunkIntoFunctionParts({ artifactChunk }) {
  return transformOneJsChunk({
    artifactChunk,
    emitParts: true,
    entryFile: artifactChunk?.entryFile,
    stageName: "splitOneJsChunkIntoFunctionParts",
  });
}

export function transformOneJsChunk({ artifactChunk, emitParts, entryFile, stageName }) {
  const entryArtifactFile = artifactChunk?.files?.find((file) => file.path === artifactChunk.entryFile);
  if (!entryArtifactFile?.ast) {
    throw new Error(
      `${stageName} requires AST for file: ${artifactChunk?.chunkId ?? "<unknown>"}/${artifactChunk?.entryFile ?? "<entry>"}`
    );
  }
  const normalizedEntryFile = normalizeChunkEntryFile(
    entryFile ?? artifactChunk?.entryFile ?? CANONICAL_CHUNK_ENTRY_FILE
  );
  const startedAt = process.hrtime.bigint();
  const jsPath = sourcePathForLoadedChunk(artifactChunk);
  const chunkId = artifactChunk.chunkId;
  const inputBytes = entryArtifactFile.content ? Buffer.byteLength(entryArtifactFile.content) : 0;
  const result = splitScopeHoistedChunkAst(entryArtifactFile.ast, {
    chunkId,
    entryFile: normalizedEntryFile,
    emitParts,
    includeJsFileAsts: true,
    sourcePath: jsPath,
  });
  return {
    chunkId,
    inputPath: jsPath,
    counts: result.manifest.counts,
    jsFiles: result.jsFiles,
    manifest: result.manifest,
    timing: {
      durationMs: Number(process.hrtime.bigint() - startedAt) / 1_000_000,
      inputBytes,
    },
  };
}

function splitChunksParallel({ workerDataByChunk, jobs }) {
  const results = new Array(workerDataByChunk.length);
  let nextIndex = 0;
  let active = 0;
  let firstError;

  return waitForAll();

  function waitForAll() {
    return new Promise((resolvePromise, rejectPromise) => {
      const startNext = () => {
        if (firstError) {
          rejectPromise(firstError);
          return;
        }
        if (nextIndex >= workerDataByChunk.length && active === 0) {
          resolvePromise(results);
          return;
        }
        while (active < jobs && nextIndex < workerDataByChunk.length) {
          const index = nextIndex++;
          active++;
          runWorker(workerDataByChunk[index])
            .then((result) => {
              results[index] = {
                ...result,
                jsFiles: new Map(result.jsFiles),
              };
            })
            .catch((error) => {
              firstError = error;
            })
            .finally(() => {
              active--;
              startNext();
            });
        }
      };
      startNext();
    });
  }
}

function runWorker(workerData) {
  return new Promise((resolvePromise, rejectPromise) => {
    const worker = new Worker(new URL("./split_function_parts_worker.mjs", import.meta.url), { workerData });
    worker.once("message", (message) => {
      if (message.ok) {
        resolvePromise(message.result);
      } else {
        const error = new Error(message.error?.message ?? "worker failed");
        error.stack = message.error?.stack;
        rejectPromise(error);
      }
    });
    worker.once("error", rejectPromise);
    worker.once("exit", (code) => {
      if (code !== 0) {
        rejectPromise(new Error(`split worker exited with code ${code}`));
      }
    });
  });
}

function buildFunctionPartsManifest(chunks) {
  return {
    schemaVersion: 1,
    counts: {
      chunks: chunks.length,
      parts: chunks.reduce((count, chunk) => count + chunk.counts.parts, 0),
      splitFunctionDeclarations: chunks.reduce((count, chunk) => count + chunk.counts.splitFunctionDeclarations, 0),
      keptTopLevelDeclarationOwners: chunks.reduce(
        (count, chunk) => count + chunk.counts.keptTopLevelDeclarationOwners,
        0
      ),
      topLevelSideEffects: chunks.reduce((count, chunk) => count + chunk.counts.topLevelSideEffects, 0),
      exportAliases: chunks.reduce((count, chunk) => count + chunk.counts.exportAliases, 0),
      unresolvedExports: chunks.reduce((count, chunk) => count + chunk.counts.unresolvedExports, 0),
    },
    chunks: chunks.map((chunk) => ({
      chunkId: chunk.chunkId,
      sourcePath: chunk.inputPath,
    })),
  };
}

function formatBytes(bytes) {
  if (bytes >= 1024 * 1024) {
    return `${(bytes / 1024 / 1024).toFixed(1)}MiB`;
  }
  if (bytes >= 1024) {
    return `${(bytes / 1024).toFixed(1)}KiB`;
  }
  return `${bytes}B`;
}

function defaultJobs() {
  return Math.max(1, Math.min(availableParallelism() - 1, 8));
}

function requireNormalizedArtifact(artifact, stageName) {
  requirePipelineArtifact(artifact, stageName);
  for (const chunk of listChunks(artifact)) {
    if (!getArtifactChunkManifest(artifact, chunk.chunkId)) {
      throw new Error(
        `${stageName} requires normalizeJsChunks first; missing chunk manifest for ${chunk.chunkId}`
      );
    }
  }
  return artifact;
}

function slowestChunks(chunks, limit) {
  return [...chunks].sort((left, right) => right.timing.durationMs - left.timing.durationMs).slice(0, limit);
}

export function normalizeAssetPath(path) {
  const normalized = path.split("\\").join("/");
  if (normalized === "" || normalized.startsWith("/") || normalized.split("/").includes("..")) {
    throw new Error(`Invalid snapshot-relative JS path: ${path}`);
  }
  if (!normalized.endsWith(".js")) {
    throw new Error(`Expected a .js path in JS list: ${path}`);
  }
  return posix.normalize(normalized);
}

export function chunkIdForJsPath(jsPath) {
  return jsPath.slice(0, -".js".length);
}

function sourcePathForLoadedChunk(chunk) {
  return normalizeAssetPath(chunk.metadata?.sourcePath ?? `${chunk.chunkId}.js`);
}
