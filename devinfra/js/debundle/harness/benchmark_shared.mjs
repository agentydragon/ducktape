import { mkdtempSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { analyzeRuntimeBoundaryAst } from "../analysis/boundary.mjs";
import { getArtifactManifest, getChunkEntryFile, listChunkIds } from "../common/artifact.mjs";
import { planSelectedAtomicModules } from "../extract/planner.mjs";

export function writeJsListForFixtureDir(fixtureDir) {
  const entries = readdirSync(fixtureDir, { withFileTypes: true })
    .filter((e) => e.isFile() && e.name.endsWith(".js"))
    .map((e) => ({ name: e.name, size: statSync(join(fixtureDir, e.name)).size }))
    .sort((l, r) => r.size - l.size);
  if (entries.length === 0) {
    throw new Error(`No JavaScript files found in fixture directory: ${fixtureDir}`);
  }
  const tmp = mkdtempSync(join(tmpdir(), "benchmark_excalidraw-"));
  const jsListPath = join(tmp, "js-files.txt");
  writeFileSync(jsListPath, `${entries[0].name}\n`);
  return jsListPath;
}

export function pickChunkWithMostTopLevelBindings(artifact) {
  const chunkIds = listChunkIds(artifact);
  if (chunkIds.length === 0) {
    throw new Error("Artifact contains no chunks; cannot pick benchmark chunk");
  }
  let bestChunkId = null;
  let bestBindings = -1;
  for (const chunkId of chunkIds) {
    const bindings = artifact.extras?.chunkManifests?.[chunkId]?.counts?.topLevelBindings ?? 0;
    if (bindings > bestBindings) {
      bestBindings = bindings;
      bestChunkId = chunkId;
    }
  }
  return bestChunkId;
}

export function buildPairLogicalModuleOperations(artifact, chunkId, moduleCount, { startAtomicModuleIndex = 0 } = {}) {
  const runtimeFile = getChunkEntryFile(artifact, chunkId);
  const analysis = analyzeRuntimeBoundaryAst(runtimeFile.ast, {
    chunkId,
    manifestPath: `${chunkId}/manifest.json`,
    runtimePath: `${chunkId}/${runtimeFile.path}`,
    uiVersion: getArtifactManifest(artifact)?.uiVersion ?? null,
  });
  const atomicPlan = planSelectedAtomicModules({ analysis, code: null, programBody: runtimeFile.ast.program.body }, {});
  const ownerById = new Map(analysis.owners.map((o) => [o.id, o]));
  const operations = [];
  for (let pairIndex = 0; pairIndex < moduleCount; pairIndex++) {
    const left = atomicPlan.modulePlans[startAtomicModuleIndex + pairIndex * 2];
    const right = atomicPlan.modulePlans[startAtomicModuleIndex + pairIndex * 2 + 1];
    if (!left || !right) break;
    operations.push({
      id: `bench_logical_${pairIndex.toString().padStart(3, "0")}`,
      operation: "define_logical_module",
      selector: { chunkId },
      target: { path: `bench/${pairIndex.toString().padStart(3, "0")}` },
      members: [left, right].map((m, ix) => createAnchorMember(m, ownerById, pairIndex, ix)),
    });
  }
  return operations;
}

function createAnchorMember(modulePlan, ownerById, pairIndex, moduleIndex) {
  const owner = ownerById.get(modulePlan.ownerIds[0]) ?? null;
  const sourceName = owner?.names?.[0] ?? modulePlan.memberNames[0];
  return {
    id: `bench_member_${pairIndex.toString().padStart(3, "0")}_${moduleIndex.toString().padStart(2, "0")}`,
    name: sourceName,
    selector: {
      binding: {
        kind: owner?.type === "VariableDeclaration" ? "VariableDeclarator" : (owner?.type ?? null),
        name: sourceName,
      },
      ...(owner ? { owner: { id: owner.id } } : {}),
    },
  };
}
