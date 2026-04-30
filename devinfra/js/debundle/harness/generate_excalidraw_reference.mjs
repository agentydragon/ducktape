#!/usr/bin/env node
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parse as parseJsonc, printParseErrorCode } from "jsonc-parser";
import { getChunk } from "../common/artifact.mjs";
import { computeJsAsts } from "../common/parse_asts.mjs";
import { loadJsChunks } from "../common/load_chunks.mjs";
import { normalizeJsChunks } from "../common/normalize.mjs";
import { materializeLogicalModules } from "../extract/materialize_logical_modules.mjs";
import { serializeGeneratedJsFile } from "../split/chunk.mjs";
import {
  buildPairLogicalModuleOperations,
  pickChunkWithMostTopLevelBindings,
  writeJsListForFixtureDir,
} from "./benchmark_shared.mjs";

const HARNESS_DIR = dirname(fileURLToPath(import.meta.url));
const DEFAULT_FIXTURE_DIR = resolve(HARNESS_DIR, "excalidraw_bundle_assets");
const DEFAULT_OUT_DIR = "devinfra/js/debundle/harness/testdata/excalidraw_reference";
const DEFAULT_SPEC_PATH = resolve(HARNESS_DIR, "testdata/excalidraw_reference/spec.jsonc");

export async function buildExcalidrawReferenceGolden(outDir, options = {}) {
  const fixtureDir = options.fixtureDir ?? DEFAULT_FIXTURE_DIR;
  const resolvedOutDir = resolve(outDir);
  const specPath = options.specPath ?? DEFAULT_SPEC_PATH;
  const spec = parseSpec(readFileSync(specPath, "utf8"), specPath);

  let artifact = loadJsChunks({ inputRoot: fixtureDir, jsListPath: writeJsListForFixtureDir(fixtureDir) }).artifact;
  artifact = (await computeJsAsts({ artifact })).artifact;
  artifact = (await normalizeJsChunks({ artifact })).artifact;
  const chunkId = pickChunkWithMostTopLevelBindings(artifact);
  const operations = buildPairLogicalModuleOperations(artifact, chunkId, spec.moduleCount, {
    startAtomicModuleIndex: spec.startAtomicModuleIndex,
  });
  artifact = (
    await materializeLogicalModules({
      artifact,
      chunkIds: [chunkId],
      operations,
      pruneOtherChunks: true,
      targetDir: "reference",
    })
  ).artifact;

  mkdirSync(resolvedOutDir, { recursive: true });
  for (const name of spec.reverseEngineeredNames) {
    rmSync(join(resolvedOutDir, name), { force: true });
  }
  const chunk = getChunk(artifact, chunkId);
  const files = [...chunk.files.values()]
    .filter((file) => file.path.startsWith("reference/"))
    .sort((l, r) => l.path.localeCompare(r.path))
    .slice(0, spec.moduleCount);

  for (const [index, file] of files.entries()) {
    const outName = spec.reverseEngineeredNames[index] ?? basename(file.path);
    const outputPath = join(resolvedOutDir, outName);
    mkdirSync(dirname(outputPath), { recursive: true });
    writeFileSync(outputPath, serializeGeneratedJsFile(file));
  }
  writeFileSync(join(resolvedOutDir, "spec.jsonc"), readFileSync(specPath, "utf8"));
}

function parseSpec(text, sourceName) {
  const errors = [];
  const parsed = parseJsonc(text, errors);
  if (errors.length > 0) {
    const details = errors.map((error) => `${printParseErrorCode(error.error)}@${error.offset}`).join(", ");
    throw new Error(`Failed to parse ${sourceName}: ${details}`);
  }
  return parsed;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await buildExcalidrawReferenceGolden(process.argv[2] ?? DEFAULT_OUT_DIR);
}
