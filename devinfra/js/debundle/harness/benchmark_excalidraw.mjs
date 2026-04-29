#!/usr/bin/env node
// Debundler benchmark on the Excalidraw bundle.
//
// Pipeline: load_js_chunks -> compute_js_asts -> normalize_js_chunks ->
//           materialize_logical_modules.
//
// The benchmark synthesizes a small set of `define_logical_module` operations
// by first planning atomic modules on the largest chunk and then grouping
// consecutive atomic modules into pairwise logical modules. This keeps the
// benchmark aligned with the current first-party materialization path while
// still exercising non-trivial module regrouping work.

import { writeFileSync } from "node:fs";
import { Session } from "node:inspector/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { computeJsAsts } from "../common/parse_asts.mjs";
import { loadJsChunks } from "../common/load_chunks.mjs";
import { normalizeJsChunks } from "../common/normalize.mjs";
import { materializeLogicalModules } from "../extract/materialize_logical_modules.mjs";
import {
  buildPairLogicalModuleOperations,
  pickChunkWithMostTopLevelBindings,
  writeJsListForFixtureDir,
} from "./benchmark_shared.mjs";

const DEFAULT_FIXTURE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "excalidraw_bundle_assets");
const DEFAULT_MODULE_COUNT = 20;

async function main() {
  const { cpuProfilePath, fixtureDir, moduleCount } = parseArgs(process.argv.slice(2));
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

  artifact = await timeStage("load_js_chunks", stageTimings, () => loadJsChunks({ inputRoot: fixtureDir, jsListPath }));
  artifact = await timeStage("compute_js_asts", stageTimings, () => computeJsAsts({ artifact }));
  artifact = await timeStage("normalize_js_chunks", stageTimings, () => normalizeJsChunks({ artifact }));

  const entryChunkId = pickChunkWithMostTopLevelBindings(artifact);
  const operations = buildPairLogicalModuleOperations(artifact, entryChunkId, moduleCount);
  artifact = await timeStage(`materialize_logical_modules x${operations.length}`, stageTimings, () =>
    materializeLogicalModules({
      artifact,
      chunkIds: [entryChunkId],
      operations,
      pruneOtherChunks: false,
      targetDir: "bench",
    })
  );

  const totalDurationMs = nsToMs(process.hrtime.bigint() - totalStartedAt);

  if (profilerSession) {
    const { profile } = await profilerSession.post("Profiler.stop");
    writeFileSync(cpuProfilePath, JSON.stringify(profile));
    await profilerSession.post("Profiler.disable");
    profilerSession.disconnect();
    process.stdout.write(`cpu profile written to ${cpuProfilePath}\n`);
  }

  reportResults({ chunkId: entryChunkId, operations, stageTimings, totalDurationMs });
}

function parseArgs(argv) {
  const result = {
    cpuProfilePath: null,
    fixtureDir: DEFAULT_FIXTURE_DIR,
    moduleCount: DEFAULT_MODULE_COUNT,
  };
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    if (arg === "--fixture-dir") {
      result.fixtureDir = resolve(requireValue(argv, ++index, arg));
    } else if (arg === "--modules") {
      result.moduleCount = parseModuleCount(requireValue(argv, ++index, arg));
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

function requireValue(argv, index, flag) {
  const value = argv[index];
  if (!value || value.startsWith("--")) {
    throw new Error(`${flag} requires a value`);
  }
  return value;
}

function parseModuleCount(value) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error(`--modules must be a positive integer, got ${value}`);
  }
  return parsed;
}

function printUsage() {
  process.stdout.write(`Usage:
  benchmark_excalidraw [--fixture-dir DIR] [--modules N]

Defaults: --fixture-dir ${DEFAULT_FIXTURE_DIR}
          --modules ${DEFAULT_MODULE_COUNT}
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

function reportResults({ chunkId, operations, stageTimings, totalDurationMs }) {
  process.stdout.write("\n=== benchmark_excalidraw results ===\n");
  process.stdout.write(`chunk: ${chunkId}\n`);
  process.stdout.write(`logical modules: ${operations.length}\n`);
  for (const { label, durationMs } of stageTimings) {
    process.stdout.write(`  ${label.padEnd(36)} ${durationMs.toFixed(1)}ms\n`);
  }
  process.stdout.write(`total: ${totalDurationMs.toFixed(1)}ms\n`);
}

function nsToMs(bigintNanoseconds) {
  return Number(bigintNanoseconds) / 1_000_000;
}

await main();
