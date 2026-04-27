// Conservative split: emits one part .js file per SCC of named top-level
// FunctionDeclarations whose entire transitive dep set is also extractable.
// Variables, classes, side-effecting statements, and any function depending
// on non-extractable code stay in the entry file. This is a strict subset
// of what extractAtomicModules handles; prefer that for richer extraction.
//
// Owns its own per-chunk worker pool. Independent of normalize: nothing in
// this file is reused by the normalize pass.

import { availableParallelism } from "node:os";
import { Worker } from "node:worker_threads";
import {
  createEmptyArtifact,
  createFile,
  getArtifactChunkManifest,
  listChunks,
  replaceChunks,
  requirePipelineArtifact,
  setArtifactChunkManifest,
  setArtifactManifest,
} from "../common/artifact.mjs";
import { formatDuration, logProgress } from "../common/io.mjs";
import { CANONICAL_CHUNK_ENTRY_FILE, normalizeAssetPath, normalizeChunkEntryFile } from "../common/normalize.mjs";
import { splitScopeHoistedChunkAst } from "./chunk.mjs";

export async function splitFunctionParts({ artifact, jobs = undefined } = {}) {
  requirePipelineArtifact(artifact, "splitFunctionParts");
  for (const chunk of listChunks(artifact)) {
    if (!getArtifactChunkManifest(artifact, chunk.chunkId)) {
      throw new Error(
        `splitFunctionParts requires normalizeJsChunks first; missing chunk manifest for ${chunk.chunkId}`
      );
    }
  }

  const sourceChunks = listChunks(artifact);
  const effectiveJobs = jobs ?? defaultJobs();
  logProgress(`split-function-parts start chunks=${sourceChunks.length} jobs=${effectiveJobs}`);
  const startedAt = process.hrtime.bigint();

  const chunks =
    effectiveJobs === 1 || sourceChunks.length <= 1
      ? sourceChunks.map((chunk) => splitOneChunk(chunk))
      : await runWorkerPool(sourceChunks, effectiveJobs);

  const nextArtifact = createEmptyArtifact();
  const manifest = buildArtifactManifest(chunks);
  const outputChunks = chunks.map((chunk) => {
    setArtifactChunkManifest(nextArtifact, chunk.chunkId, chunk.manifest);
    return {
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
      metadata: { sourcePath: chunk.inputPath },
    };
  });
  replaceChunks(nextArtifact, outputChunks);
  setArtifactManifest(nextArtifact, manifest);

  const durationMs = Number(process.hrtime.bigint() - startedAt) / 1_000_000;
  logProgress(
    `split-function-parts done chunks=${manifest.counts.chunks} parts=${manifest.counts.parts} duration=${formatDuration(durationMs)}`
  );
  for (const slow of slowestChunks(chunks, 8)) {
    logProgress(
      `split-function-parts slow-chunk chunk=${slow.chunkId} duration=${formatDuration(slow.timing.durationMs)} parts=${slow.counts.parts} keptOwners=${slow.counts.keptTopLevelDeclarationOwners}`
    );
  }
  return { artifact: nextArtifact, manifest };
}

export function splitOneChunk(artifactChunk) {
  const entryArtifactFile = artifactChunk?.files?.find((file) => file.path === artifactChunk.entryFile);
  if (!entryArtifactFile?.ast) {
    throw new Error(
      `splitOneChunk requires AST for chunk: ${artifactChunk?.chunkId ?? "<unknown>"}/${artifactChunk?.entryFile ?? "<entry>"}`
    );
  }
  const startedAt = process.hrtime.bigint();
  const inputPath = normalizeAssetPath(artifactChunk.metadata?.sourcePath ?? `${artifactChunk.chunkId}.js`);
  const inputBytes = entryArtifactFile.content ? Buffer.byteLength(entryArtifactFile.content) : 0;
  const result = splitScopeHoistedChunkAst(entryArtifactFile.ast, {
    chunkId: artifactChunk.chunkId,
    entryFile: normalizeChunkEntryFile(artifactChunk.entryFile ?? CANONICAL_CHUNK_ENTRY_FILE),
    includeJsFileAsts: true,
    sourcePath: inputPath,
  });
  return {
    chunkId: artifactChunk.chunkId,
    inputPath,
    counts: result.manifest.counts,
    jsFiles: result.jsFiles,
    manifest: result.manifest,
    timing: {
      durationMs: Number(process.hrtime.bigint() - startedAt) / 1_000_000,
      inputBytes,
    },
  };
}

function buildArtifactManifest(chunks) {
  return {
    schemaVersion: 1,
    counts: {
      chunks: chunks.length,
      parts: chunks.reduce((n, c) => n + c.counts.parts, 0),
      splitFunctionDeclarations: chunks.reduce((n, c) => n + c.counts.splitFunctionDeclarations, 0),
      keptTopLevelDeclarationOwners: chunks.reduce((n, c) => n + c.counts.keptTopLevelDeclarationOwners, 0),
      topLevelSideEffects: chunks.reduce((n, c) => n + c.counts.topLevelSideEffects, 0),
      exportAliases: chunks.reduce((n, c) => n + c.counts.exportAliases, 0),
      unresolvedExports: chunks.reduce((n, c) => n + c.counts.unresolvedExports, 0),
    },
    chunks: chunks.map((c) => ({ chunkId: c.chunkId, sourcePath: c.inputPath })),
  };
}

function slowestChunks(chunks, limit) {
  return [...chunks].sort((left, right) => right.timing.durationMs - left.timing.durationMs).slice(0, limit);
}

function defaultJobs() {
  return Math.max(1, Math.min(availableParallelism() - 1, 8));
}

function runWorkerPool(sourceChunks, jobs) {
  const results = new Array(sourceChunks.length);
  let nextIndex = 0;
  let active = 0;
  let firstError;
  return new Promise((resolvePromise, rejectPromise) => {
    const startNext = () => {
      if (firstError) {
        rejectPromise(firstError);
        return;
      }
      if (nextIndex >= sourceChunks.length && active === 0) {
        resolvePromise(results);
        return;
      }
      while (active < jobs && nextIndex < sourceChunks.length) {
        const index = nextIndex++;
        active++;
        runWorker(sourceChunks[index])
          .then((result) => {
            results[index] = { ...result, jsFiles: new Map(result.jsFiles) };
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

function runWorker(artifactChunk) {
  return new Promise((resolvePromise, rejectPromise) => {
    const worker = new Worker(new URL("./split_worker.mjs", import.meta.url), {
      workerData: { artifactChunk },
    });
    worker.once("message", (message) => {
      if (message.ok) {
        resolvePromise(message.result);
      } else {
        const error = new Error(message.error?.message ?? "split worker failed");
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
