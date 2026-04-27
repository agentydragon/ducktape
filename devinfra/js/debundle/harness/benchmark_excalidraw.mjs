#!/usr/bin/env node
// Debundler benchmark on the Excalidraw bundle.
//
// Pipeline: load_js_chunks -> compute_js_asts -> normalize_js_chunks ->
//           extract_atomic_modules -> merge_modules x N (default 20).
//
// Atom IDs are read from the artifact after extract_atomic_modules; merges
// pair-fold consecutive atoms (atom_0+atom_1, atom_2+atom_3, ...) on the
// chunk that produced the most atoms. Times each stage and total wall.
//
// The Excalidraw bundle is sourced via Bazel from the digest-pinned
// excalidraw/excalidraw OCI image (see devinfra/image_pins.json) and
// extracted at build time by //devinfra/oci:extract_image_subdir. The
// extracted directory lives next to this script in runfiles.

import { mkdtempSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { Session } from "node:inspector/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { computeJsAsts } from "../common/compute_js_asts_lib.mjs";
import { loadJsChunks } from "../common/load_js_chunks_lib.mjs";
import { getArtifactChunkManifest, listChunkIds } from "../common/pipeline_artifact_lib.mjs";
import { extractAtomicModules } from "../extract/atomic_modules_stage_lib.mjs";
import { mergeModules } from "../extract/merge_modules_stage_lib.mjs";
import { normalizeJsChunks } from "../split/split_function_parts_lib.mjs";

const DEFAULT_FIXTURE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "excalidraw_bundle_assets");
const DEFAULT_MERGE_COUNT = 20;

async function main() {
  const { cpuProfilePath, fixtureDir, mergeCount } = parseArgs(process.argv.slice(2));
  const jsListPath = writeJsListForFixtureDir(fixtureDir);

  const profilerSession = cpuProfilePath ? new Session() : null;
  if (profilerSession) {
    profilerSession.connect();
    await profilerSession.post("Profiler.enable");
    await profilerSession.post("Profiler.setSamplingInterval", { interval: 100 });
    await profilerSession.post("Profiler.start");
  }

  const totalStartedAt = process.hrtime.bigint();
  const stageTimings = [];
  let artifact;

  artifact = await timeStage("load_js_chunks", stageTimings, () =>
    loadJsChunks({ inputRoot: fixtureDir, jsListPath })
  );
  artifact = await timeStage("compute_js_asts", stageTimings, () => computeJsAsts({ artifact }));
  artifact = await timeStage("normalize_js_chunks", stageTimings, () => normalizeJsChunks({ artifact }));

  // Production usage extracts atoms from a single entry chunk at a time;
  // pick the largest chunk by source bytes as a proxy for the entry chunk.
  const entryChunkId = pickLargestChunkId(artifact);
  artifact = await timeStage("extract_atomic_modules", stageTimings, () =>
    extractAtomicModules({ artifact, chunkIds: [entryChunkId], pruneOtherChunks: false })
  );

  const { chunkId, atomIds } = pickBenchmarkChunk(artifact);
  const operations = buildPairMergeOperations(chunkId, atomIds, mergeCount);
  if (operations.length === 0) {
    throw new Error(`No merge operations could be synthesized (atomIds=${atomIds.length})`);
  }

  artifact = await timeStage(`merge_modules x${operations.length}`, stageTimings, () =>
    mergeModules({ artifact, operations })
  );

  const totalDurationMs = nsToMs(process.hrtime.bigint() - totalStartedAt);

  if (profilerSession) {
    const { profile } = await profilerSession.post("Profiler.stop");
    writeFileSync(cpuProfilePath, JSON.stringify(profile));
    await profilerSession.post("Profiler.disable");
    profilerSession.disconnect();
    process.stdout.write(`cpu profile written to ${cpuProfilePath}\n`);
  }

  reportResults({ chunkId, atomIds, operations, stageTimings, totalDurationMs });
}

function parseArgs(argv) {
  const result = {
    cpuProfilePath: null,
    fixtureDir: DEFAULT_FIXTURE_DIR,
    mergeCount: DEFAULT_MERGE_COUNT,
  };
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    if (arg === "--fixture-dir") {
      result.fixtureDir = resolve(requireValue(argv, ++index, arg));
    } else if (arg === "--merges") {
      result.mergeCount = parseMergeCount(requireValue(argv, ++index, arg));
    } else if (arg === "--cpu-profile") {
      result.cpuProfilePath = resolve(requireValue(argv, ++index, arg));
    } else if (arg === "--help" || arg === "-h") {
      printUsage();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return result;
}

function writeJsListForFixtureDir(fixtureDir) {
  // Pick only the largest .js file by on-disk size as a stand-in for the
  // deployed entry chunk. Production usage extracts atoms from a single
  // entry chunk at a time; loading + parsing the other 50+ chunks just to
  // discard them inflates the benchmark wall by several seconds.
  const entries = readdirSync(fixtureDir, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".js"))
    .map((entry) => ({ name: entry.name, size: statSync(join(fixtureDir, entry.name)).size }))
    .sort((left, right) => right.size - left.size);
  if (entries.length === 0) {
    throw new Error(`No .js files found under ${fixtureDir}`);
  }
  const tmp = mkdtempSync(join(tmpdir(), "benchmark_excalidraw-"));
  const jsListPath = join(tmp, "js-files.txt");
  writeFileSync(jsListPath, `${entries[0].name}\n`);
  return jsListPath;
}

function requireValue(argv, index, flag) {
  const value = argv[index];
  if (!value || value.startsWith("--")) {
    throw new Error(`${flag} requires a value`);
  }
  return value;
}

function parseMergeCount(value) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error(`--merges must be a positive integer, got ${value}`);
  }
  return parsed;
}

function printUsage() {
  process.stdout.write(`Usage:
  benchmark_excalidraw [--fixture-dir DIR] [--merges N]

Defaults: --fixture-dir ${DEFAULT_FIXTURE_DIR}
          --merges ${DEFAULT_MERGE_COUNT}
`);
}

async function timeStage(label, timings, runStage) {
  const startedAt = process.hrtime.bigint();
  const result = await runStage();
  const durationMs = nsToMs(process.hrtime.bigint() - startedAt);
  timings.push({ label, durationMs });
  process.stdout.write(`stage ${label} ${durationMs.toFixed(1)}ms\n`);
  return result.artifact;
}

function pickLargestChunkId(artifact) {
  let bestChunkId = null;
  let bestBytes = -1;
  for (const chunkId of listChunkIds(artifact)) {
    const manifest = getArtifactChunkManifest(artifact, chunkId);
    const bytes = manifest?.counts?.topLevelBindings ?? 0;
    if (bytes > bestBytes) {
      bestBytes = bytes;
      bestChunkId = chunkId;
    }
  }
  if (!bestChunkId) {
    throw new Error("No chunks loaded");
  }
  return bestChunkId;
}

function pickBenchmarkChunk(artifact) {
  let bestChunkId = null;
  let bestAtomIds = [];
  for (const chunkId of listChunkIds(artifact)) {
    const manifest = getArtifactChunkManifest(artifact, chunkId);
    const atomIds = manifest?.atomicModules?.moduleIds ?? [];
    if (atomIds.length > bestAtomIds.length) {
      bestChunkId = chunkId;
      bestAtomIds = atomIds;
    }
  }
  if (!bestChunkId) {
    throw new Error("No chunk had any atomic modules");
  }
  return { chunkId: bestChunkId, atomIds: bestAtomIds };
}

function buildPairMergeOperations(chunkId, atomIds, mergeCount) {
  const operations = [];
  for (let pairIndex = 0; pairIndex < mergeCount; pairIndex++) {
    const left = atomIds[pairIndex * 2];
    const right = atomIds[pairIndex * 2 + 1];
    if (left === undefined || right === undefined) {
      break;
    }
    operations.push({
      id: `bench_merge_${pairIndex.toString().padStart(3, "0")}`,
      operation: "merge_module",
      selector: { chunkId, moduleIds: [left, right] },
      target: { basename: `bench_merge_${pairIndex.toString().padStart(3, "0")}` },
    });
  }
  return operations;
}

function reportResults({ chunkId, atomIds, operations, stageTimings, totalDurationMs }) {
  process.stdout.write("\n=== benchmark_excalidraw results ===\n");
  process.stdout.write(`chunk: ${chunkId}\n`);
  process.stdout.write(`atoms: ${atomIds.length}\n`);
  process.stdout.write(`merges: ${operations.length}\n`);
  for (const { label, durationMs } of stageTimings) {
    process.stdout.write(`  ${label.padEnd(32)} ${durationMs.toFixed(1)}ms\n`);
  }
  process.stdout.write(`total: ${totalDurationMs.toFixed(1)}ms\n`);
}

function nsToMs(bigintNanoseconds) {
  return Number(bigintNanoseconds) / 1_000_000;
}

await main();
