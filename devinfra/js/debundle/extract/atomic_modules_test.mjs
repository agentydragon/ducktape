import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { parse } from "@babel/parser";
import { analyzeRuntimeBoundaryAst, analyzeRuntimeBoundaryCode } from "../analysis/boundary.mjs";
import { computeJsAsts } from "../common/parse_asts.mjs";
import { getArtifactChunkManifest, getChunk, getChunkEntryFile, setArtifactManifest } from "../common/artifact.mjs";
import { DEFAULT_PARSER_OPTIONS } from "../common/parser_options.mjs";
import { loadJsChunks } from "../common/load_chunks.mjs";
import { normalizeJsChunks } from "../common/normalize.mjs";
import { createWebFixtureRoots, runNodeScript, writeSnapshotFixture } from "../test_support/fixtures.mjs";
import { runTransformSpecObject } from "../transforms/runner.mjs";
import { writeJsTree } from "../transforms/write.mjs";
import { serializeGeneratedJsFile } from "../split/chunk.mjs";
import { extractAtomicModules } from "./atomic_modules.mjs";
import { materializeLogicalModules } from "./materialize_logical_modules.mjs";
import { mergeModules } from "./merge.mjs";

test("extractAtomicModules emits one module per atomic unit and preserves behavior", async () => {
  const { artifact, selectedOwnerIds, snapshotRoot } = await prepareAtomicFixture("debundle-atomic-modules-stage-");

  const result = extractAtomicModules({
    artifact,
    chunkIds: ["static/app"],
    pruneOtherChunks: false,
    selectedOwnerIdsByChunk: {
      "static/app": selectedOwnerIds,
    },
  });

  const chunk = getChunk(result.artifact, "static/app");
  const state = chunk?.metadata?.moduleExtractionState;
  assert.equal(result.manifest.kind, "js.atomic_module_manifest");
  assert.ok(state);
  assert.equal(state.kind, "js.module_extraction_state");
  assert.equal(state.currentModules.length, state.atomicUnits.length);
  assert.ok(state.currentModules.length > 1);
  assert.deepEqual(
    state.currentModules.map((modulePlan) => modulePlan.unitIds),
    state.currentModules.map((modulePlan) => [modulePlan.id.replace(/^atomic_module_/, "selected_atomic_unit_")])
  );
  assert.equal(
    [...chunk.files.keys()].filter((file) => file.startsWith("modules/")).length,
    state.currentModules.length
  );
  const firstModuleFile = chunk.files.get(state.currentModules[0].targetFile);
  assert.equal(
    firstModuleFile?.headerLines?.[0],
    "// @ducktape-generated kind=lowerer-helper stage=selected_module_lowering ignore=detectors"
  );
  assert.deepEqual(firstModuleFile?.metadata?.generated, {
    kind: "lowerer_helper",
    stage: "selected_module_lowering",
    generator: "devinfra/js/debundle/extract/init_region.mjs",
    ignoreByDefault: true,
  });

  const { outRoot } = createWebFixtureRoots("debundle-atomic-modules-stage-write-");
  writeJsTree({
    artifact: result.artifact,
    force: true,
    outDir: outRoot,
  });

  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("mergeModules merges selected extracted modules and preserves behavior", async () => {
  const { artifact, selectedOwnerIds, snapshotRoot } = await prepareAtomicFixture("debundle-merge-modules-stage-");
  const extracted = extractAtomicModules({
    artifact,
    chunkIds: ["static/app"],
    pruneOtherChunks: false,
    selectedOwnerIdsByChunk: {
      "static/app": selectedOwnerIds,
    },
  });
  const stateBefore = getChunk(extracted.artifact, "static/app")?.metadata?.moduleExtractionState;
  assert.ok(stateBefore);
  assert.ok(stateBefore.currentModules.length >= 3);

  const merged = mergeModules({
    artifact: extracted.artifact,
    operations: [
      {
        id: "merge__seed_and_first",
        operation: "merge_module",
        selector: {
          chunkId: "static/app",
          moduleIds: [stateBefore.currentModules[0].id, stateBefore.currentModules[1].id],
        },
        target: {
          path: "seed_and_first",
        },
      },
    ],
  });

  const chunk = getChunk(merged.artifact, "static/app");
  const stateAfter = chunk?.metadata?.moduleExtractionState;
  assert.ok(stateAfter);
  assert.equal(stateAfter.currentModules.length, stateBefore.currentModules.length - 1);
  assert.ok(stateAfter.currentModules.some((modulePlan) => modulePlan.id === "merge__seed_and_first"));
  assert.ok(chunk.files.has("modules/seed_and_first.js"));

  const { outRoot } = createWebFixtureRoots("debundle-merge-modules-stage-write-");
  writeJsTree({
    artifact: merged.artifact,
    force: true,
    outDir: outRoot,
  });

  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("mergeModules resolves moduleSelectors by exact member-name sets", async () => {
  const { artifact, selectedOwnerIds, snapshotRoot } = await prepareAtomicFixture("debundle-merge-modules-symbol-selectors-");
  const extracted = extractAtomicModules({
    artifact,
    chunkIds: ["static/app"],
    pruneOtherChunks: false,
    selectedOwnerIdsByChunk: {
      "static/app": selectedOwnerIds,
    },
  });
  const stateBefore = getChunk(extracted.artifact, "static/app")?.metadata?.moduleExtractionState;
  assert.ok(stateBefore);
  assert.ok(stateBefore.currentModules.length >= 3);

  const merged = mergeModules({
    artifact: extracted.artifact,
    operations: [
      {
        id: "merge__seed_and_first",
        operation: "merge_module",
        selector: {
          chunkId: "static/app",
          moduleSelectors: [
            moduleSelectorForModulePlan(stateBefore.currentModules[0]),
            {
              ...moduleSelectorForModulePlan(stateBefore.currentModules[1]),
              nearbyStructure: {
                nextSymbols: [...stateBefore.currentModules[2].memberNames],
                previousSymbols: [...stateBefore.currentModules[0].memberNames],
              },
              ordinalWindow: {
                end: stateBefore.currentModules[1].startOrdinal,
                start: stateBefore.currentModules[1].startOrdinal,
              },
            },
          ],
          validation: {
            ordered: true,
          },
        },
        target: {
          path: "seed_and_first",
        },
      },
    ],
  });

  const chunk = getChunk(merged.artifact, "static/app");
  const stateAfter = chunk?.metadata?.moduleExtractionState;
  assert.ok(stateAfter);
  assert.equal(stateAfter.currentModules.length, stateBefore.currentModules.length - 1);
  assert.ok(stateAfter.currentModules.some((modulePlan) => modulePlan.id === "merge__seed_and_first"));
  assert.ok(chunk.files.has("modules/seed_and_first.js"));

  const { outRoot } = createWebFixtureRoots("debundle-merge-modules-symbol-selectors-write-");
  writeJsTree({
    artifact: merged.artifact,
    force: true,
    outDir: outRoot,
  });

  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("mergeModules matches exact selector symbols against the full current member-name set", async () => {
  const { artifact, selectedOwnerIds, snapshotRoot } = await prepareAtomicFixture("debundle-merge-modules-full-set-selectors-");
  const extracted = extractAtomicModules({
    artifact,
    chunkIds: ["static/app"],
    pruneOtherChunks: false,
    selectedOwnerIdsByChunk: {
      "static/app": selectedOwnerIds,
    },
  });
  const stateBefore = getChunk(extracted.artifact, "static/app")?.metadata?.moduleExtractionState;
  assert.ok(stateBefore);
  assert.ok(stateBefore.currentModules.length >= 4);

  const mergedSeedPair = mergeModules({
    artifact: extracted.artifact,
    operations: [
      {
        id: "merge__seed_pair",
        operation: "merge_module",
        selector: {
          chunkId: "static/app",
          moduleIds: [stateBefore.currentModules[0].id, stateBefore.currentModules[1].id],
        },
        target: {
          path: "seed_pair",
        },
      },
    ],
  });
  const stateAfterSeedPair = getChunk(mergedSeedPair.artifact, "static/app")?.metadata?.moduleExtractionState;
  assert.ok(stateAfterSeedPair);

  const merged = mergeModules({
    artifact: mergedSeedPair.artifact,
    operations: [
      {
        id: "merge__seed_and_first",
        operation: "merge_module",
        selector: {
          chunkId: "static/app",
          moduleSelectors: [
            { symbols: ["readSeed", "seed"] },
            { symbols: ["first"] },
          ],
          validation: {
            ordered: true,
          },
        },
        target: {
          path: "seed_and_first",
        },
      },
    ],
  });

  const chunk = getChunk(merged.artifact, "static/app");
  const stateAfter = chunk?.metadata?.moduleExtractionState;
  assert.ok(stateAfter);
  assert.equal(stateAfter.currentModules.length, stateAfterSeedPair.currentModules.length - 1);
  assert.ok(stateAfter.currentModules.some((modulePlan) => modulePlan.id === "merge__seed_and_first"));
  assert.ok(chunk.files.has("modules/seed_and_first.js"));

  const { outRoot } = createWebFixtureRoots("debundle-merge-modules-full-set-selectors-write-");
  writeJsTree({
    artifact: merged.artifact,
    force: true,
    outDir: outRoot,
  });

  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("mergeModules resolves representative symbol subsets against the full current member-name set", async () => {
  const { artifact, selectedOwnerIds, snapshotRoot } = await prepareAtomicFixture(
    "debundle-merge-modules-representative-symbol-selectors-"
  );
  const extracted = extractAtomicModules({
    artifact,
    chunkIds: ["static/app"],
    pruneOtherChunks: false,
    selectedOwnerIdsByChunk: {
      "static/app": selectedOwnerIds,
    },
  });
  const stateBefore = getChunk(extracted.artifact, "static/app")?.metadata?.moduleExtractionState;
  assert.ok(stateBefore);
  assert.ok(stateBefore.currentModules.length >= 4);

  const mergedSeedPair = mergeModules({
    artifact: extracted.artifact,
    operations: [
      {
        id: "merge__seed_pair",
        operation: "merge_module",
        selector: {
          chunkId: "static/app",
          moduleIds: [stateBefore.currentModules[0].id, stateBefore.currentModules[1].id],
        },
        target: {
          path: "seed_pair",
        },
      },
    ],
  });
  const stateAfterSeedPair = getChunk(mergedSeedPair.artifact, "static/app")?.metadata?.moduleExtractionState;
  assert.ok(stateAfterSeedPair);

  const merged = mergeModules({
    artifact: mergedSeedPair.artifact,
    operations: [
      {
        id: "merge__seed_and_first",
        operation: "merge_module",
        selector: {
          chunkId: "static/app",
          moduleSelectors: [
            { symbols: ["seed"] },
            { symbols: ["first"] },
          ],
          validation: {
            ordered: true,
          },
        },
        target: {
          path: "seed_and_first",
        },
      },
    ],
  });

  const chunk = getChunk(merged.artifact, "static/app");
  const stateAfter = chunk?.metadata?.moduleExtractionState;
  assert.ok(stateAfter);
  assert.equal(stateAfter.currentModules.length, stateAfterSeedPair.currentModules.length - 1);
  assert.ok(stateAfter.currentModules.some((modulePlan) => modulePlan.id === "merge__seed_and_first"));
  assert.ok(chunk.files.has("modules/seed_and_first.js"));

  const { outRoot } = createWebFixtureRoots("debundle-merge-modules-representative-symbol-selectors-write-");
  writeJsTree({
    artifact: merged.artifact,
    force: true,
    outDir: outRoot,
  });

  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("mergeModules ordered selector validation rejects reversed module selector order", async () => {
  const { artifact, selectedOwnerIds } = await prepareAtomicFixture("debundle-merge-modules-selector-order-");
  const extracted = extractAtomicModules({
    artifact,
    chunkIds: ["static/app"],
    pruneOtherChunks: false,
    selectedOwnerIdsByChunk: {
      "static/app": selectedOwnerIds,
    },
  });
  const stateBefore = getChunk(extracted.artifact, "static/app")?.metadata?.moduleExtractionState;
  assert.ok(stateBefore);
  assert.ok(stateBefore.currentModules.length >= 3);

  assert.throws(
    () =>
      mergeModules({
        artifact: extracted.artifact,
        operations: [
          {
            id: "merge__seed_and_first",
            operation: "merge_module",
            selector: {
              chunkId: "static/app",
              moduleSelectors: [
                moduleSelectorForModulePlan(stateBefore.currentModules[1]),
                moduleSelectorForModulePlan(stateBefore.currentModules[0]),
              ],
              validation: {
                ordered: true,
              },
            },
            target: {
              path: "seed_and_first",
            },
          },
        ],
      }),
    /ordered moduleSelectors did not match ascending startOrdinal order/
  );
});

test("mergeModules writes post-merge reports with before/after counts", async () => {
  const { artifact, selectedOwnerIds } = await prepareAtomicFixture("debundle-merge-modules-report-");
  const extracted = extractAtomicModules({
    artifact,
    chunkIds: ["static/app"],
    pruneOtherChunks: false,
    selectedOwnerIdsByChunk: {
      "static/app": selectedOwnerIds,
    },
  });
  const stateBefore = getChunk(extracted.artifact, "static/app")?.metadata?.moduleExtractionState;
  assert.ok(stateBefore);

  const { outRoot } = createWebFixtureRoots("debundle-merge-modules-report-write-");
  const reportOutDir = join(outRoot, "reports");
  const merged = mergeModules({
    artifact: extracted.artifact,
    operations: [
      {
        id: "merge__seed_and_first",
        operation: "merge_module",
        selector: {
          chunkId: "static/app",
          moduleIds: [stateBefore.currentModules[0].id, stateBefore.currentModules[1].id],
        },
        target: {
          path: "seed_and_first",
        },
      },
    ],
    reportOutDir,
    reportSummaryPath: join(reportOutDir, "summary.json"),
  });

  assert.equal(merged.manifest.kind, "js.merge_module_manifest");
  assert.equal(existsSync(join(reportOutDir, "summary.json")), true);
  assert.equal(existsSync(join(reportOutDir, "static", "app.json")), true);

  const summary = JSON.parse(readFileSync(join(reportOutDir, "summary.json"), "utf8"));
  const chunkReport = JSON.parse(readFileSync(join(reportOutDir, "static", "app.json"), "utf8"));
  assert.equal(summary.counts.chunks, 1);
  assert.equal(summary.counts.mergeOperations, 1);
  assert.equal(summary.counts.modulesBefore, stateBefore.currentModules.length);
  assert.equal(summary.counts.modulesAfter, stateBefore.currentModules.length - 1);
  assert.equal(summary.counts.mergedAway, 1);
  assert.equal(chunkReport.counts.modulesBefore, stateBefore.currentModules.length);
  assert.equal(chunkReport.counts.modulesAfter, stateBefore.currentModules.length - 1);
  assert.deepEqual(chunkReport.operationIds, ["merge__seed_and_first"]);
});

test("merge_remaining_modules folds all unclaimed modules into one residual module", async () => {
  const { artifact, selectedOwnerIds, snapshotRoot } = await prepareAtomicFixture("debundle-merge-remaining-modules-");
  const extracted = extractAtomicModules({
    artifact,
    chunkIds: ["static/app"],
    pruneOtherChunks: false,
    selectedOwnerIdsByChunk: {
      "static/app": selectedOwnerIds,
    },
  });
  const stateBefore = getChunk(extracted.artifact, "static/app")?.metadata?.moduleExtractionState;
  assert.ok(stateBefore);
  assert.ok(stateBefore.currentModules.length >= 3);

  const merged = mergeModules({
    artifact: extracted.artifact,
    operations: [
      {
        id: "merge__seed_and_first",
        operation: "merge_module",
        selector: {
          chunkId: "static/app",
          moduleIds: [stateBefore.currentModules[0].id, stateBefore.currentModules[1].id],
        },
        target: {
          path: "seed_and_first",
        },
      },
      {
        id: "merge__unhandled",
        operation: "merge_remaining_modules",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
  });

  const chunk = getChunk(merged.artifact, "static/app");
  const stateAfter = chunk?.metadata?.moduleExtractionState;
  assert.ok(stateAfter);
  assert.equal(stateAfter.currentModules.length, 2);
  assert.ok(stateAfter.currentModules.some((modulePlan) => modulePlan.id === "merge__seed_and_first"));
  assert.ok(stateAfter.currentModules.some((modulePlan) => modulePlan.id === "merge__unhandled"));
  assert.ok(chunk.files.has("modules/residual/unhandled.js"));

  const { outRoot } = createWebFixtureRoots("debundle-merge-remaining-modules-write-");
  writeJsTree({
    artifact: merged.artifact,
    force: true,
    outDir: outRoot,
  });

  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules lowers final logical modules directly from combined ops", async () => {
  const { artifact, snapshotRoot } = await prepareAtomicFixture("debundle-materialize-logical-modules-stage-");
  const operations = logicalModuleOpsForFixture();

  const materialized = materializeLogicalModules({
    artifact,
    chunkIds: ["static/app"],
    operations,
    pruneOtherChunks: false,
  });

  const chunk = getChunk(materialized.artifact, "static/app");
  const state = chunk?.metadata?.moduleExtractionState;
  assert.ok(state);
  assert.equal(state.mode, "logical");
  assert.equal(state.currentModules.length, 3);
  assert.ok(chunk.files.has("modules/state/seed_state.js"));
  assert.ok(chunk.files.has("modules/state/first_state.js"));
  assert.ok(chunk.files.has("modules/residual/unhandled.js"));
  assert.equal(materialized.manifest.kind, "js.logical_module_manifest");
  assert.equal(materialized.manifest.counts.blockedMembers, 0);
  assert.equal(materialized.manifest.counts.explicitLogicalModules, 2);
  assert.equal(materialized.manifest.counts.residualLogicalModules, 1);
  assert.deepEqual(
    materialized.manifest.chunks[0].finalModuleContents.map((modulePlan) => modulePlan.path),
    ["state/seed_state", "state/first_state", "residual/unhandled"]
  );

  const { outRoot } = createWebFixtureRoots("debundle-materialize-logical-modules-stage-write-");
  writeJsTree({
    artifact: materialized.artifact,
    force: true,
    outDir: outRoot,
  });

  const seedModuleCode = readFileSync(join(outRoot, "static", "app", "modules", "state", "seed_state.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");
  assert.match(seedModuleCode, /\bseedValue\b/);
  assert.match(seedModuleCode, /\breadSeedValue\b/);
  assert.match(seedModuleCode, /\bconst seedValue = 1;/);
  assert.match(seedModuleCode, /\bfunction readSeedValue\(\)/);
  assert.doesNotMatch(seedModuleCode, /\b__dt_generated_init__state_seed_state\b/);
  assert.doesNotMatch(seedModuleCode, /\bexport let seed\b/);
  assert.doesNotMatch(entryCode, /\bseedValue\b/);
  assert.doesNotMatch(entryCode, /\breadSeedValue\b/);
  assert.doesNotMatch(entryCode, /\b__dt_generated_init__state_seed_state\b/);

  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules writes reusable boundary-analysis and selected-owner cache artifacts", async () => {
  const { artifact } = await prepareAtomicFixture("debundle-materialize-logical-modules-cache-");
  const operations = logicalModuleOpsForFixture();
  const { outRoot } = createWebFixtureRoots("debundle-materialize-logical-modules-cache-write-");
  const boundaryAnalysisDir = join(outRoot, "analysis", "boundary");
  const selectedOwnerIdsByChunkPath = join(outRoot, "analysis", "selected-owner-ids.json");

  const materialized = materializeLogicalModules({
    artifact,
    boundaryAnalysisDir,
    chunkIds: ["static/app"],
    operations,
    pruneOtherChunks: false,
    selectedOwnerIdsByChunkPath,
  });

  assert.equal(materialized.manifest.kind, "js.logical_module_manifest");
  assert.equal(existsSync(join(boundaryAnalysisDir, "static/app.json")), true);
  assert.equal(existsSync(selectedOwnerIdsByChunkPath), true);

  const selectedOwnerCache = JSON.parse(readFileSync(selectedOwnerIdsByChunkPath, "utf8"));
  assert.equal(selectedOwnerCache.kind, "js.selected_owner_ids_cache");
  assert.ok(Array.isArray(selectedOwnerCache.chunkOwnerIds["static/app"]));
  assert.ok(selectedOwnerCache.chunkOwnerIds["static/app"].length > 0);

  const cachedAnalysis = JSON.parse(readFileSync(join(boundaryAnalysisDir, "static/app.json"), "utf8"));
  assert.equal(cachedAnalysis.kind, "js.runtime_boundary_analysis");
});

test("materializeLogicalModules allows nested logical module paths with colliding leaf basenames", async () => {
  const { artifact } = await prepareAtomicFixture("debundle-materialize-logical-modules-nested-collisions-");
  const operations = [
    {
      id: "logical__state_core",
      operation: "define_logical_module",
      selector: {
        chunkId: "static/app",
      },
      target: {
        path: "state/core",
      },
      members: [
        {
          id: "rename__seed",
          name: "seedValue",
          selector: {
            binding: {
              kind: "VariableDeclarator",
              name: "seed",
            },
          },
        },
      ],
    },
    {
      id: "logical__search_core",
      operation: "define_logical_module",
      selector: {
        chunkId: "static/app",
      },
      target: {
        path: "search/core",
      },
      members: [
        {
          id: "rename__first",
          name: "firstValue",
          selector: {
            binding: {
              kind: "VariableDeclarator",
              name: "first",
            },
          },
        },
      ],
    },
    {
      id: "logical__unhandled",
      operation: "define_residual_module",
      selector: {
        chunkId: "static/app",
      },
      target: {
        path: "residual/unhandled",
      },
    },
  ];

  const materialized = materializeLogicalModules({
    artifact,
    chunkIds: ["static/app"],
    operations,
    pruneOtherChunks: false,
  });

  const chunk = getChunk(materialized.artifact, "static/app");
  assert.ok(chunk.files.has("modules/state/core.js"));
  assert.ok(chunk.files.has("modules/search/core.js"));
  assert.ok(chunk.files.has("modules/residual/unhandled.js"));
});

test("materializeLogicalModules propagates final names through emitted imports and exports", async () => {
  const { artifact } = await prepareAtomicFixture("debundle-materialize-logical-modules-final-name-propagation-");
  const materialized = materializeLogicalModules({
    artifact,
    chunkIds: ["static/app"],
    operations: logicalModuleOpsForFixture(),
    pruneOtherChunks: false,
  });

  const chunk = getChunk(materialized.artifact, "static/app");
  const seedModuleCode = readFileSyncFromArtifactFile(chunk.files.get("modules/state/seed_state.js"));
  const firstModuleCode = readFileSyncFromArtifactFile(chunk.files.get("modules/state/first_state.js"));
  const entryCode = readFileSyncFromArtifactFile(chunk.files.get("entry.js"));

  assert.match(seedModuleCode, /\bconst seedValue = 1;/);
  assert.match(seedModuleCode, /\bfunction readSeedValue\(\)/);
  assert.doesNotMatch(seedModuleCode, /export function __dt_generated_init__state_seed_state\(\)/);
  assert.match(seedModuleCode, /export \{ readSeedValue \};/);
  assert.doesNotMatch(seedModuleCode, /export \{ seedValue/);
  assert.doesNotMatch(seedModuleCode, /export \{ .* as .* \}/);
  assert.match(firstModuleCode, /import \{ readSeedValue \} from "\.\/seed_state\.js";/);
  assert.doesNotMatch(firstModuleCode, /import \{ .* as .* \} from "\.\/seed_state\.js";/);
  assert.match(entryCode, /import "\.\/modules\/state\/seed_state\.js";/);
  assert.doesNotMatch(entryCode, /import \{[^}]*seedValue[^}]*\} from "\.\/modules\/state\/seed_state\.js";/);
  assert.doesNotMatch(entryCode, /import \{[^}]*readSeedValue[^}]*\} from "\.\/modules\/state\/seed_state\.js";/);
  assert.match(entryCode, /import \{ firstValue, readFirstValue, __dt_generated_init__state_first_state \} from "\.\/modules\/state\/first_state\.js";/);
  assert.doesNotMatch(entryCode, /import \{ .* as .* \} from "\.\/modules\/state\/first_state\.js";/);
});

test("materializeLogicalModules renames destructured object params to readable property names", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-readable-object-params-"
  );
  const source = `function c7t({ resourceIds: n, startDateTime: e }) {
  return n.join(",") + ":" + e;
}
function readQuery() {
  return c7t({
    resourceIds: ["a", "b"],
    startDateTime: "2024-01-01T00:00:00Z",
  });
}
console.log(readQuery());
export { c7t, readQuery };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__resource_query",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "commands/search/resource_query",
        },
        members: [
          {
            id: "member__build_query",
            name: "buildResourceQuery",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "c7t",
              },
            },
          },
          {
            id: "member__read_query",
            name: "readResourceQuery",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "readQuery",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "parse",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  const moduleCode = readFileSync(
    join(outRoot, "static", "app", "modules", "commands", "search", "resource_query.js"),
    "utf8"
  );
  assert.match(moduleCode, /function buildResourceQuery\(\{\s*resourceIds,\s*startDateTime\s*\}\)/s);
  assert.match(moduleCode, /return resourceIds\.join\(","\) \+ ":" \+ startDateTime;/);
  assert.doesNotMatch(moduleCode, /\{ resourceIds: n, startDateTime: e \}/);
  assert.doesNotMatch(moduleCode, /resourceIds: resourceIds/);
  assert.doesNotMatch(moduleCode, /startDateTime: startDateTime/);
  assert.doesNotMatch(moduleCode, /return n\.join\(","\) \+ ":" \+ e;/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules renames function-expression params even when returned object shorthand reuses the same bindings", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-readable-function-expression-params-"
  );
  const source = `let _Wt;
_Wt = async function _Wt({ nodeSpace: n, text: e, tagsAsPromptSection: t }) {
  return {
    nodeSpace: n,
    text: e,
    tagsAsPromptSection: t,
  };
};
async function readPayload() {
  return _Wt({
    nodeSpace: "space",
    text: "hello",
    tagsAsPromptSection: true,
  });
}
console.log(JSON.stringify(await readPayload()));
export { _Wt, readPayload };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__tag_candidate_input",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "commands/ai/tag_candidate_input",
        },
        members: [
          {
            id: "member__build_input",
            name: "buildTagCandidateInput",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "_Wt",
              },
            },
          },
          {
            id: "member__read_payload",
            name: "readPayload",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "readPayload",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "parse",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  const moduleCode = readFileSync(
    join(outRoot, "static", "app", "modules", "commands", "ai", "tag_candidate_input.js"),
    "utf8"
  );
  assert.match(
    moduleCode,
    /buildTagCandidateInput = async function _Wt\(\{\s*nodeSpace,\s*text,\s*tagsAsPromptSection\s*\}\)/s
  );
  assert.match(moduleCode, /return \{\s*nodeSpace,\s*text,\s*tagsAsPromptSection\s*\};/s);
  assert.doesNotMatch(moduleCode, /\{\s*nodeSpace: n,\s*text: e,\s*tagsAsPromptSection: t\s*\}/s);
  assert.doesNotMatch(moduleCode, /return \{\s*nodeSpace: n,\s*text: e,\s*tagsAsPromptSection: t\s*\};/s);
  assert.deepEqual(
    runNodeScript(join(outRoot, "static", "app", "entry.js")),
    runNodeScript(join(snapshotRoot, "static", "app.js"))
  );
});

test("materializeLogicalModules renames simple object destructuring statements to readable locals", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-readable-object-statements-"
  );
  const source = `function r7t(n) {
  const { hostNode: e, hostParent: t } = n;
  return e.id + ":" + t.id;
}
function s7t(payload) {
  return r7t(payload);
}
export { r7t, s7t };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__host_reader",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "ui/host/reader",
        },
        members: [
          {
            id: "member__build_host_path",
            name: "buildHostPath",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "r7t",
              },
            },
          },
          {
            id: "member__read_host_path",
            name: "readHostPath",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "s7t",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "parse",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  const moduleCode = readFileSync(join(outRoot, "static", "app", "modules", "ui", "host", "reader.js"), "utf8");
  const normalizedModuleCode = moduleCode
    .replace(/^\/\/ @ducktape-generated.*\n/gm, "")
    .replace(/^\/\/ @ducktape-generator.*\n/gm, "")
    .replace(/^\/\/ Selected-module lowered region.*\n/gm, "")
    .replace(/\/\/ Selected-module lowered region.*?(?=function|export|let|const|class)/g, "")
    .replace(/\/\* @ducktape-atomic-boundary[^*]*\*\//g, "")
    .replace(/\s+/g, " ")
    .trim();
  assert.equal(
    normalizedModuleCode,
    `function buildHostPath(n) { const { hostNode, hostParent } = n; return hostNode.id + ":" + hostParent.id; } function readHostPath(payload) { return buildHostPath(payload); } export { buildHostPath, readHostPath };`
  );
  assert.doesNotMatch(moduleCode, /hostNode: e/);
  assert.doesNotMatch(moduleCode, /hostParent: t/);
  assert.doesNotMatch(moduleCode, /return e\.id \+ ":" \+ t\.id;/);
  assert.deepEqual(
    runNodeScript(join(outRoot, "static", "app", "entry.js")),
    runNodeScript(join(snapshotRoot, "static", "app.js"))
  );
});

test("materializeLogicalModules renames return-object aliases to readable shorthand locals", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-readable-return-object-"
  );
  const source = `function r8t(n) {
  const includeTitle = n.includeTitle;
  const l = n.includeDescription;
  return {
    includeTitle: includeTitle,
    includeDescription: l,
  };
}
function s8t(payload) {
  return r8t(payload);
}
export { r8t, s8t };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__metadata_options",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "ui/metadata/options",
        },
        members: [
          {
            id: "member__build_metadata_options",
            name: "buildMetadataOptions",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "r8t",
              },
            },
          },
          {
            id: "member__read_metadata_options",
            name: "readMetadataOptions",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "s8t",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "parse",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  const moduleCode = readFileSync(join(outRoot, "static", "app", "modules", "ui", "metadata", "options.js"), "utf8");
  const normalizedModuleCode = moduleCode
    .replace(/^\/\/ @ducktape-generated.*\n/gm, "")
    .replace(/^\/\/ @ducktape-generator.*\n/gm, "")
    .replace(/^\/\/ Selected-module lowered region.*\n/gm, "")
    .replace(/\/\/ Selected-module lowered region.*?(?=function|export|let|const|class)/g, "")
    .replace(/\/\* @ducktape-atomic-boundary[^*]*\*\//g, "")
    .replace(/\s+/g, " ")
    .trim();
  assert.equal(
    normalizedModuleCode,
    `function buildMetadataOptions(n) { const includeTitle = n.includeTitle; const includeDescription = n.includeDescription; return { includeTitle, includeDescription }; } function readMetadataOptions(payload) { return buildMetadataOptions(payload); } export { buildMetadataOptions, readMetadataOptions };`
  );
  assert.doesNotMatch(moduleCode, /includeTitle: includeTitle/);
  assert.doesNotMatch(moduleCode, /includeDescription: l/);
  assert.deepEqual(
    runNodeScript(join(outRoot, "static", "app", "entry.js")),
    runNodeScript(join(snapshotRoot, "static", "app.js"))
  );
});

test("materializeLogicalModules rejects propagated final-name collisions in consumer modules", async () => {
  const { artifact } = await prepareAtomicFixture("debundle-materialize-logical-modules-propagated-collision-");
  assert.throws(
    () =>
      materializeLogicalModules({
        artifact,
        chunkIds: ["static/app"],
        operations: [
          {
            id: "logical__seed_state",
            operation: "define_logical_module",
            selector: {
              chunkId: "static/app",
            },
            target: {
              path: "state/seed_state",
            },
            members: [
              {
                id: "rename__seed",
                name: "seedValue",
                selector: {
                  binding: {
                    kind: "VariableDeclarator",
                    name: "seed",
                  },
                },
              },
              {
                id: "rename__readSeed",
                name: "first",
                selector: {
                  binding: {
                    kind: "FunctionDeclaration",
                    name: "readSeed",
                  },
                },
              },
            ],
          },
          {
            id: "logical__first_state",
            operation: "define_logical_module",
            selector: {
              chunkId: "static/app",
            },
            target: {
              path: "state/first_state",
            },
            members: [
              {
                id: "rename__first",
                name: "first",
                selector: {
                  binding: {
                    kind: "VariableDeclarator",
                    name: "first",
                  },
                },
              },
              {
                id: "rename__readFirst",
                name: "readFirstValue",
                selector: {
                  binding: {
                    kind: "FunctionDeclaration",
                    name: "readFirst",
                  },
                },
              },
            ],
          },
          {
            id: "logical__unhandled",
            operation: "define_residual_module",
            selector: {
              chunkId: "static/app",
            },
            target: {
              path: "residual/unhandled",
            },
          },
        ],
        pruneOtherChunks: false,
      }),
    /propagated final name collision|conflicts with existing top-level binding|duplicate binding name|duplicate exported logical names/i
  );
});

test("materializeLogicalModules emits atomic boundary comments for merged logical modules", async () => {
  const { artifact } = await prepareAtomicFixture("debundle-materialize-logical-modules-boundary-markers-");
  const materialized = materializeLogicalModules({
    artifact,
    chunkIds: ["static/app"],
    operations: [
      {
        id: "logical__state_all",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "state/all_state",
        },
        members: [
          {
            id: "rename__seed",
            name: "seedValue",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "seed",
              },
            },
          },
          {
            id: "rename__readSeed",
            name: "readSeedValue",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "readSeed",
              },
            },
          },
          {
            id: "rename__first",
            name: "firstValue",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "first",
              },
            },
          },
          {
            id: "rename__readFirst",
            name: "readFirstValue",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "readFirst",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pruneOtherChunks: false,
  });

  const chunk = getChunk(materialized.artifact, "static/app");
  const mergedModuleCode = readFileSyncFromArtifactFile(chunk.files.get("modules/state/all_state.js"));
  assert.match(mergedModuleCode, /@ducktape-atomic-boundary kind=selected_module_lowering id=atomic_module_/);
  assert.match(mergedModuleCode, /members=seed\b/);
  assert.match(mergedModuleCode, /members=readSeed\b/);
  assert.match(mergedModuleCode, /members=first\b/);
  assert.match(mergedModuleCode, /members=readFirst\b/);
});

test("materializeLogicalModules can split pure multi-declarator top-level constants into fragment-backed modules", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-pure-variable-fragments-"
  );
  const source = `const alpha = "a", beta = "b";
function readAlpha() {
  return alpha;
}
function readBeta() {
  return beta;
}

console.log(readAlpha(), readBeta());

export { readAlpha, readBeta };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__alpha",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "constants/alpha",
        },
        members: [
          {
            id: "member__alpha",
            name: "alphaConstant",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "alpha",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );

  const alphaCode = readFileSync(join(outRoot, "static", "app", "modules", "constants", "alpha.js"), "utf8");
  const residualCode = readFileSync(join(outRoot, "static", "app", "modules", "residual", "unhandled.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");

  assert.match(alphaCode, /\bconst alphaConstant = "a";/);
  assert.match(alphaCode, /\balphaConstant\b/);
  assert.doesNotMatch(alphaCode, /\bbeta\b/);
  assert.match(alphaCode, /fragments=owner_[^,\s]+::declarator_0/);
  assert.doesNotMatch(alphaCode, /export function __dt_generated_init__/);
  assert.doesNotMatch(alphaCode, /\n\s*alphaConstant = "a";/);

  assert.match(residualCode, /\bbeta = "b"/);
  assert.doesNotMatch(residualCode, /\balphaConstant\b/);
  assert.match(residualCode, /fragments=owner_[^,\s]+::declarator_1/);

  assert.match(entryCode, /modules\/constants\/alpha\.js/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules lowers pure constant fragments without init wrappers", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-plain-constant-fragment-pipeline-"
  );
  const source = `const OVt = 500, fallbackTimeoutMs = 250;
export { OVt, fallbackTimeoutMs };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__request_timeout",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "runtime/request_timeout",
        },
        members: [
          {
            id: "member__request_timeout",
            name: "requestTimeoutMs",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "OVt",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );

  const timeoutCode = readFileSync(join(outRoot, "static", "app", "modules", "runtime", "request_timeout.js"), "utf8");
  const residualCode = readFileSync(join(outRoot, "static", "app", "modules", "residual", "unhandled.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");

  assert.match(timeoutCode, /\bconst requestTimeoutMs = 500;/);
  assert.match(timeoutCode, /fragments=owner_[^,\s]+::declarator_0/);
  assert.doesNotMatch(timeoutCode, /export function __dt_generated_init__/);
  assert.doesNotMatch(timeoutCode, /\n\s*requestTimeoutMs = 500;/);

  assert.match(residualCode, /\bconst fallbackTimeoutMs = 250;/);
  assert.match(entryCode, /import \{ requestTimeoutMs \} from "\.\/modules\/runtime\/request_timeout\.js";/);
  assert.doesNotMatch(entryCode, /__dt_generated_init__runtime_request_timeout/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules lowers multi-stage pure declaration modules without init wrappers", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-plain-multi-stage-pipeline-"
  );
  const source = `const OVt = 500;
const gapLabel = "gap";
function readTimeout() {
  return OVt;
}
export { OVt, gapLabel, readTimeout };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__request_timeout",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "runtime/request_timeout",
        },
        members: [
          {
            id: "member__request_timeout",
            name: "requestTimeoutMs",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "OVt",
              },
            },
          },
          {
            id: "member__read_timeout",
            name: "readRequestTimeoutMs",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "readTimeout",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );

  const timeoutCode = readFileSync(join(outRoot, "static", "app", "modules", "runtime", "request_timeout.js"), "utf8");
  const residualCode = readFileSync(join(outRoot, "static", "app", "modules", "residual", "unhandled.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");

  assert.match(timeoutCode, /\bconst requestTimeoutMs = 500;/);
  assert.match(timeoutCode, /\bfunction readRequestTimeoutMs\(\)/);
  assert.doesNotMatch(timeoutCode, /export function __dt_generated_init__/);
  assert.doesNotMatch(timeoutCode, /\n\s*requestTimeoutMs = 500;/);

  assert.match(residualCode, /\bconst gapLabel = "gap";/);
  assert.match(entryCode, /import \{ requestTimeoutMs, readRequestTimeoutMs \} from "\.\/modules\/runtime\/request_timeout\.js";/);
  assert.doesNotMatch(entryCode, /__dt_generated_init__runtime_request_timeout/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules lowers self-contained snapshot variable owners without init wrappers", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-self-contained-snapshot-pipeline-"
  );
  const source = `var Status = ((Status2) => {
  Status2.Idle = "idle";
  Status2.Busy = "busy";
  return Status2;
})(Status || {});
function readStatus() {
  return Status.Busy;
}
export { readStatus };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__status",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "runtime/status",
        },
        members: [
          {
            id: "member__status",
            name: "ChangeStatus",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "Status",
              },
            },
          },
          {
            id: "member__read_status",
            name: "readChangeStatus",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "readStatus",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );

  const statusCode = readFileSync(join(outRoot, "static", "app", "modules", "runtime", "status.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");

  assert.match(statusCode, /\bvar ChangeStatus = \(Status2 =>/);
  assert.match(statusCode, /\bfunction readChangeStatus\(\)/);
  assert.doesNotMatch(statusCode, /__dt_selected_module_snapshot__/);
  assert.doesNotMatch(statusCode, /export function __dt_generated_init__/);
  assert.match(statusCode, /export \{ readChangeStatus \};/);
  assert.doesNotMatch(statusCode, /export \{[^\n}]*\bChangeStatus\b/);
  assert.match(entryCode, /import \{ readChangeStatus \} from "\.\/modules\/runtime\/status\.js";/);
  assert.doesNotMatch(entryCode, /import \{[^}]*\bChangeStatus\b[^}]*\} from "\.\/modules\/runtime\/status\.js";/);
  assert.doesNotMatch(entryCode, /__dt_generated_init__runtime_status/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules can split pure destructuring declarators into fragment-backed modules", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-destructuring-fragments-"
  );
  const source = `const { focus: focusLabel } = { focus: "focus" }, otherLabel = "other";
function readFocusLabel() {
  return focusLabel;
}
function readOtherLabel() {
  return otherLabel;
}

console.log(readFocusLabel(), readOtherLabel());

export { readFocusLabel, readOtherLabel };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__focus_label",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "ui/focus/label",
        },
        members: [
          {
            id: "member__focus_label",
            name: "focusServiceLabel",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "focusLabel",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );

  const focusCode = readFileSync(join(outRoot, "static", "app", "modules", "ui", "focus", "label.js"), "utf8");
  const residualCode = readFileSync(join(outRoot, "static", "app", "modules", "residual", "unhandled.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");

  assert.match(focusCode, /\bconst\s*\{\s*focus: focusServiceLabel\s*\}\s*=\s*\{\s*focus: "focus"\s*\};/s);
  assert.doesNotMatch(focusCode, /\botherLabel\b/);
  assert.match(focusCode, /fragments=owner_[^,\s]+::declarator_0/);

  assert.match(residualCode, /\botherLabel = "other"/);
  assert.doesNotMatch(residualCode, /\bfocusServiceLabel\b/);
  assert.match(residualCode, /fragments=owner_[^,\s]+::declarator_1/);

  assert.match(entryCode, /modules\/ui\/focus\/label\.js/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules can split declarator fragments even when an attached side effect touches only one fragment", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-side-effect-fragments-"
  );
  const source = `globalThis.window = {};
const createPlatformClient = () => ({ ok: true }), indexedDbOutgoingTxSendQueue = new Map();
window.indexedDbSendQueue = indexedDbOutgoingTxSendQueue;
function reportQueueSize() {
  return indexedDbOutgoingTxSendQueue.size;
}
function reportPlatformClient() {
  return createPlatformClient().ok;
}

console.log(reportQueueSize(), reportPlatformClient());

export { reportPlatformClient, reportQueueSize };`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__platform_client",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "platform/client",
        },
        members: [
          {
            id: "member__create_platform_client",
            name: "createPlatformClient",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "createPlatformClient",
              },
            },
          },
        ],
      },
      {
        id: "logical__outgoing_tx_debug",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "sync/outgoing_tx/debug",
        },
        members: [
          {
            id: "member__indexed_db_queue",
            name: "indexedDbOutgoingTxSendQueue",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "indexedDbOutgoingTxSendQueue",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );

  const platformCode = readFileSync(join(outRoot, "static", "app", "modules", "platform", "client.js"), "utf8");
  const outgoingTxCode = readFileSync(join(outRoot, "static", "app", "modules", "sync", "outgoing_tx", "debug.js"), "utf8");
  const residualCode = readFileSync(join(outRoot, "static", "app", "modules", "residual", "unhandled.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");

  assert.match(platformCode, /\bcreatePlatformClient\b/);
  assert.doesNotMatch(platformCode, /\bindexedDbOutgoingTxSendQueue\b/);
  assert.match(platformCode, /fragments=owner_[^,\s]+::declarator_0/);

  assert.match(outgoingTxCode, /\bindexedDbOutgoingTxSendQueue\b/);
  assert.doesNotMatch(outgoingTxCode, /\bcreatePlatformClient\b/);
  assert.match(outgoingTxCode, /fragments=owner_[^,\s]+::declarator_group_1/);
  assert.match(outgoingTxCode, /window\.indexedDbSendQueue = indexedDbOutgoingTxSendQueue/);

  assert.match(entryCode, /modules\/platform\/client\.js/);
  assert.match(entryCode, /modules\/sync\/outgoing_tx\/debug\.js/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules closes explicit modules over selected helper dependencies", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-helper-closure-"
  );
  const source = `const symbolFor = (name, existing) => ((existing = Symbol[name]) ? existing : Symbol.for("Symbol." + name)),
  failExpected = (message) => {
    throw TypeError(message);
  };
const addDisposableResource = (stack, value, isAsync) => {
    if (value != null) {
      typeof value != "object" && typeof value != "function" && failExpected("Object expected");
      let dispose;
      isAsync && (dispose = value[symbolFor("asyncDispose")]);
      dispose === void 0 && (dispose = value[symbolFor("dispose")]);
      typeof dispose != "function" && failExpected("Object not disposable");
      stack.push([isAsync, dispose, value]);
    } else if (isAsync) stack.push([isAsync]);
    return value;
  },
  disposeResources = (stack, error, didSuppress) => {
    const SuppressedErrorCtor =
      typeof SuppressedError == "function"
        ? SuppressedError
        : function (inner, outer, message, created) {
            return ((created = Error(message)), (created.name = "SuppressedError"), (created.error = inner), (created.suppressed = outer), created);
          };
    const suppress = (inner) =>
      (error = didSuppress ? new SuppressedErrorCtor(inner, error, "An error was suppressed during disposal") : ((didSuppress = true), inner));
    const run = (record) => {
      for (; (record = stack.pop()); )
        try {
          const result = record[1] && record[1].call(record[2]);
          if (record[0]) return Promise.resolve(result).then(run, (inner) => (suppress(inner), run()));
        } catch (inner) {
          suppress(inner);
        }
      if (didSuppress) throw error;
    };
    return run();
  };
function buildDisposableStack() {
  const stack = [];
  addDisposableResource(stack, {
    [Symbol.dispose]() {},
  }, false);
  return stack;
}
function drainDisposableStack() {
  return disposeResources(buildDisposableStack(), void 0, false);
}

console.log(typeof drainDisposableStack);

export { drainDisposableStack };`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__runtime_disposal",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "runtime/disposal",
        },
        members: [
          {
            id: "member__add_disposable_resource",
            name: "addDisposableResource",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "addDisposableResource",
              },
            },
          },
          {
            id: "member__dispose_resources",
            name: "disposeResources",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "disposeResources",
              },
            },
          },
        ],
      },
      {
        id: "logical__residual",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  const disposalCode = readFileSync(join(outRoot, "static", "app", "modules", "runtime", "disposal.js"), "utf8");
  const residualCode = readFileSync(join(outRoot, "static", "app", "modules", "residual", "unhandled.js"), "utf8");

  assert.match(disposalCode, /\baddDisposableResource\b/);
  assert.match(disposalCode, /\bdisposeResources\b/);
  assert.match(disposalCode, /\bsymbolFor\b/);
  assert.match(disposalCode, /\bfailExpected\b/);
  assert.doesNotMatch(residualCode, /\bsymbolFor\b/);
  assert.doesNotMatch(residualCode, /\bfailExpected\b/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules can split lazy callable declarators and grouped remainders from one top-level declaration", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-lazy-callable-fragments-"
  );
  const source = `const alpha = "a", buildLabel = function buildLabel() { return "label:" + alpha; }, delta = new Map([["value", 7]]);
function readDelta() {
  return delta.get("value");
}
function render() {
  return buildLabel() + ":" + readDelta();
}

console.log(render());

export { render };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const loaded = loadJsChunks({
    inputRoot: snapshotRoot,
    jsListPath: join(extractedRoot, "js-files.txt"),
  });
  computeJsAsts({ artifact: loaded.artifact });
  await normalizeJsChunks({
    artifact: loaded.artifact,
    jobs: 1,
  });
  const entryAst = getChunkEntryFile(loaded.artifact, "static/app")?.ast;
  assert.ok(entryAst);
  const analysis = analyzeRuntimeBoundaryAst(entryAst, { chunkId: "static/app" });
  const selectedOwnerIdsByChunk = {
    "static/app": analysis.owners.map((owner) => owner.id),
  };

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__alpha",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "constants/alpha",
        },
        members: [
          {
            id: "member__alpha",
            name: "alphaConstant",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "alpha",
              },
            },
          },
        ],
      },
      {
        id: "logical__label",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "labels/build_label",
        },
        members: [
          {
            id: "member__build_label",
            name: "buildLabel",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "buildLabel",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
          selectedOwnerIdsByChunk,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );

  const alphaCode = readFileSync(join(outRoot, "static", "app", "modules", "constants", "alpha.js"), "utf8");
  const labelCode = readFileSync(join(outRoot, "static", "app", "modules", "labels", "build_label.js"), "utf8");
  const residualCode = readFileSync(join(outRoot, "static", "app", "modules", "residual", "unhandled.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");

  assert.match(alphaCode, /\balphaConstant\b/);
  assert.match(alphaCode, /fragments=owner_[^,\s]+::declarator_0/);

  assert.match(labelCode, /\bbuildLabel\b/);
  assert.match(labelCode, /fragments=owner_[^,\s]+::declarator_1/);
  assert.match(labelCode, /import \{ alphaConstant \} from "\.\.\/constants\/alpha\.js"/);

  assert.match(residualCode, /\bdelta = new Map/);
  assert.match(residualCode, /fragments=owner_[^,\s]+::declarator_group_2/);

  assert.match(entryCode, /modules\/constants\/alpha\.js/);
  assert.match(entryCode, /modules\/labels\/build_label\.js/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules can extract an unrelated lazy declarator from an owner with eager intra-owner dependencies", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-eager-dependency-group-fragments-"
  );
  const source = `const beta = function beta() { return "b"; }, alpha = beta.name, gamma = function gamma() { return "g"; };
function render() {
  return gamma() + ":" + beta();
}

console.log(render());

export { render };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const loaded = loadJsChunks({
    inputRoot: snapshotRoot,
    jsListPath: join(extractedRoot, "js-files.txt"),
  });
  computeJsAsts({ artifact: loaded.artifact });
  await normalizeJsChunks({
    artifact: loaded.artifact,
    jobs: 1,
  });
  const entryAst = getChunkEntryFile(loaded.artifact, "static/app")?.ast;
  assert.ok(entryAst);
  const analysis = analyzeRuntimeBoundaryAst(entryAst, { chunkId: "static/app" });
  const selectedOwnerIdsByChunk = {
    "static/app": analysis.owners.map((owner) => owner.id),
  };

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__gamma",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "runtime/gamma",
        },
        members: [
          {
            id: "member__gamma",
            name: "renderGamma",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "gamma",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
          selectedOwnerIdsByChunk,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );

  const gammaCode = readFileSync(join(outRoot, "static", "app", "modules", "runtime", "gamma.js"), "utf8");
  const residualCode = readFileSync(join(outRoot, "static", "app", "modules", "residual", "unhandled.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");

  assert.match(gammaCode, /\brenderGamma\b/);
  assert.match(gammaCode, /fragments=owner_[^,\s]+::declarator_2/);
  assert.match(residualCode, /\bbeta = function beta/);
  assert.match(residualCode, /alpha = beta\.name/);
  assert.match(residualCode, /fragments=owner_[^,\s]+::declarator_0,owner_[^,\s]+::declarator_group_1/);
  assert.match(entryCode, /modules\/runtime\/gamma\.js/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules can split inert class declarators with lazy cross-fragment reads", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-lazy-class-fragments-"
  );
  const source = `const beta = "b", Delta = class Delta {
  static label() {
    return beta + ":" + Delta.name;
  }
};
function render() {
  return Delta.label();
}

console.log(render());

export { render };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__delta",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "classes/delta",
        },
        members: [
          {
            id: "member__delta",
            name: "Delta",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "Delta",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );

  const deltaCode = readFileSync(join(outRoot, "static", "app", "modules", "classes", "delta.js"), "utf8");
  const residualCode = readFileSync(join(outRoot, "static", "app", "modules", "residual", "unhandled.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");

  assert.match(deltaCode, /\bDelta = class Delta/);
  assert.match(deltaCode, /fragments=owner_[^,\s]+::declarator_1/);
  assert.match(deltaCode, /import \{ beta \} from "\.\.\/residual\/unhandled\.js"/);

  assert.match(residualCode, /\bbeta = "b"/);
  assert.doesNotMatch(residualCode, /\bDelta = class Delta/);
  assert.match(residualCode, /fragments=owner_[^,\s]+::declarator_0/);

  assert.match(entryCode, /modules\/classes\/delta\.js/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules can split passive declarator fragments away from lazy sibling member writes", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-lazy-member-write-fragments-"
  );
  const source = `const KU = {}, ype = function ype() {
  KU.value = 1;
};
function render() {
  ype();
  return KU.value;
}

console.log(render());

export { render };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__ku_state",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "runtime/ku_state",
        },
        members: [
          {
            id: "member__ku",
            name: "KU",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "KU",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );

  const kuCode = readFileSync(join(outRoot, "static", "app", "modules", "runtime", "ku_state.js"), "utf8");
  const residualCode = readFileSync(join(outRoot, "static", "app", "modules", "residual", "unhandled.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");

  assert.match(kuCode, /\bKU = \{\};/);
  assert.doesNotMatch(kuCode, /\bype = function ype/);
  assert.match(kuCode, /fragments=owner_[^,\s]+::declarator_0/);

  assert.match(residualCode, /\bype = function ype\(\)/);
  assert.doesNotMatch(residualCode, /\bKU = \{\};/);
  assert.match(residualCode, /import \{ KU \} from "\.\.\/runtime\/ku_state\.js"/);
  assert.match(residualCode, /fragments=owner_[^,\s]+::declarator_1/);

  assert.match(entryCode, /modules\/runtime\/ku_state\.js/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules can peel an independent declarator away from a lazy sibling rebind", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-lazy-rebind-split-"
  );
  const source = `let count = 0, bump = function bump() {
  count += 1;
  return count;
}, gamma = "g";

function readGamma() {
  return gamma;
}

function readCount() {
  return count;
}

console.log(readGamma(), readCount());

export { readGamma, readCount };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__gamma",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "runtime/gamma_only",
        },
        members: [
          {
            id: "member__gamma",
            name: "gamma",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "gamma",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );

  const gammaCode = readFileSync(join(outRoot, "static", "app", "modules", "runtime", "gamma_only.js"), "utf8");
  const residualCode = readFileSync(join(outRoot, "static", "app", "modules", "residual", "unhandled.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");

  assert.match(gammaCode, /\bgamma = "g";/);
  assert.doesNotMatch(gammaCode, /\bcount = 0,/);
  assert.doesNotMatch(gammaCode, /\bbump = function bump/);
  assert.match(gammaCode, /fragments=owner_[^,\s]+::declarator_2/);

  assert.match(residualCode, /\bcount = 0;/);
  assert.match(residualCode, /\bbump = function bump\(\)/);
  assert.doesNotMatch(residualCode, /\bgamma = "g";/);
  assert.match(residualCode, /fragments=owner_[^,\s]+::declarator_0,owner_[^,\s]+::declarator_1/);

  assert.match(entryCode, /modules\/runtime\/gamma_only\.js/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules resolves stale owner hints through analysis before selecting split declaration fragments", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-stale-owner-hints-"
  );
  const source = `const palette = "picker", pickerStyles = { root: palette };
const aliasMap = { js: "javascript" };
function renderPicker() {
  return pickerStyles.root + ":" + aliasMap.js;
}

console.log(renderPicker());

export { renderPicker };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__language_picker",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "ui/code/language_picker",
        },
        members: [
          {
            id: "member__picker_styles",
            name: "pickerStyles",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "pickerStyles",
              },
              owner: {
                id: "owner_stale_picker_styles",
                line: 1,
              },
            },
          },
          {
            id: "member__alias_map",
            name: "languageAliasMap",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "aliasMap",
              },
              owner: {
                id: "owner_stale_alias_map",
                line: 2,
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );

  const languagePickerCode = readFileSync(
    join(outRoot, "static", "app", "modules", "ui", "code", "language_picker.js"),
    "utf8"
  );
  const residualCode = readFileSync(
    join(outRoot, "static", "app", "modules", "residual", "unhandled.js"),
    "utf8"
  );

  assert.match(languagePickerCode, /\bpalette = "picker"/);
  assert.match(languagePickerCode, /\bpickerStyles = \{\s+root: palette\s+\};/);
  assert.match(languagePickerCode, /\blanguageAliasMap = \{\s+js: "javascript"\s+\};/);
  assert.doesNotMatch(languagePickerCode, /import \{ .*pickerStyles.* \} from "\.\.\/\.\.\/residual\/unhandled\.js"/);

  assert.match(residualCode, /import \{ languageAliasMap, pickerStyles \} from "\.\.\/ui\/code\/language_picker\.js";/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules rejects propagated final-name collisions for split class declarators", async () => {
  const { extractedRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-materialize-logical-modules-class-propagated-collision-"
  );
  const source = `const beta = "b", Delta = class Delta {
  static label() {
    return beta + ":" + Delta.name;
  }
};
function readDelta() {
  return Delta.label();
}
export { readDelta };`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });
  const loaded = loadJsChunks({
    inputRoot: snapshotRoot,
    jsListPath: join(extractedRoot, "js-files.txt"),
  });
  computeJsAsts({ artifact: loaded.artifact });
  const normalized = await normalizeJsChunks({
    artifact: loaded.artifact,
    jobs: 1,
  });

  assert.throws(
    () =>
      materializeLogicalModules({
        artifact: normalized.artifact,
        chunkIds: ["static/app"],
        operations: [
          {
            id: "logical__delta",
            operation: "define_logical_module",
            selector: {
              chunkId: "static/app",
            },
            target: {
              path: "classes/delta",
            },
            members: [
              {
                id: "rename__delta",
                name: "beta",
                selector: {
                  binding: {
                    kind: "VariableDeclarator",
                    name: "Delta",
                  },
                },
              },
              {
                id: "rename__readDelta",
                name: "readDeltaValue",
                selector: {
                  binding: {
                    kind: "FunctionDeclaration",
                    name: "readDelta",
                  },
                },
              },
            ],
          },
          {
            id: "logical__unhandled",
            operation: "define_residual_module",
            selector: {
              chunkId: "static/app",
            },
            target: {
              path: "residual/unhandled",
            },
          },
        ],
        pruneOtherChunks: false,
      }),
    /propagated final name collision|conflicts with existing top-level binding|duplicate binding name|duplicate exported logical names/i
  );
});

test("extractAtomicModules does not require a root artifact manifest", async () => {
  const { artifact, selectedOwnerIds } = await prepareAtomicFixture("debundle-atomic-modules-without-root-manifest-");
  setArtifactManifest(artifact, null);

  const extracted = extractAtomicModules({
    artifact,
    chunkIds: ["static/app"],
    pruneOtherChunks: false,
    selectedOwnerIdsByChunk: {
      "static/app": selectedOwnerIds,
    },
  });

  const chunk = getChunk(extracted.artifact, "static/app");
  assert.ok(chunk);
  assert.ok(chunk.files.has("entry.js"));
  assert.ok([...chunk.files.keys()].some((file) => file.startsWith("modules/")));
});

test("materializeLogicalModules does not require a root artifact manifest", async () => {
  const { artifact } = await prepareAtomicFixture("debundle-logical-modules-without-root-manifest-");
  setArtifactManifest(artifact, null);

  const materialized = materializeLogicalModules({
    artifact,
    chunkIds: ["static/app"],
    operations: logicalModuleOpsForFixture(),
    pruneOtherChunks: false,
  });

  const chunk = getChunk(materialized.artifact, "static/app");
  assert.ok(chunk);
  assert.ok(chunk.files.has("entry.js"));
  assert.ok(chunk.files.has("modules/state/seed_state.js"));
  assert.ok(chunk.files.has("modules/state/first_state.js"));
});

test("materializeLogicalModules can emit logical modules directly at the chunk root", async () => {
  const { artifact } = await prepareAtomicFixture("debundle-logical-modules-root-target-dir-");

  const materialized = materializeLogicalModules({
    artifact,
    chunkIds: ["static/app"],
    operations: logicalModuleOpsForFixture(),
    pruneOtherChunks: false,
    targetDir: null,
  });

  const chunk = getChunk(materialized.artifact, "static/app");
  assert.ok(chunk);
  assert.ok(chunk.files.has("entry.js"));
  assert.ok(chunk.files.has("state/seed_state.js"));
  assert.ok(chunk.files.has("state/first_state.js"));
  assert.ok(chunk.files.has("residual/unhandled.js"));
  assert.equal(chunk.files.has("modules/state/seed_state.js"), false);
  const chunkManifest = getArtifactChunkManifest(materialized.artifact, "static/app");
  assert.equal(chunkManifest.logicalModules.targetDir, null);
});

test("materialize_logical_modules composes directly in a pipeline spec", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = await writeAtomicSnapshotFixture(
    "debundle-logical-modules-pipeline-"
  );
  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: logicalModuleOpsForFixture(),
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
          targetDir: null,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );
  assert.equal(existsSync(join(outRoot, "static", "app", "modules")), false);
  assert.equal(existsSync(join(outRoot, "static", "app", "state", "seed_state.js")), true);
  assert.equal(existsSync(join(outRoot, "static", "app", "residual", "unhandled.js")), true);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materialize_logical_modules pipeline emits natural component-facing names", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-component-pipeline-"
  );
  const source = `const buttonLabel = "Approve";
function ToolApprovalRequest() {
  return \`<button>\${buttonLabel}</button>\`;
}

console.log("component-barrier");

function renderDialog() {
  return ToolApprovalRequest();
}

console.log(renderDialog());

export { ToolApprovalRequest, renderDialog };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__request",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "ui/ai/tool_approval/request",
        },
        members: [
          {
            id: "member__button_label",
            name: "toolApprovalButtonLabel",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "buttonLabel",
              },
            },
          },
          {
            id: "member__request",
            name: "ToolApprovalRequest",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "ToolApprovalRequest",
              },
            },
          },
        ],
      },
      {
        id: "logical__dialog",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "ui/ai/tool_approval/dialog",
        },
        members: [
          {
            id: "member__dialog",
            name: "renderToolApprovalDialog",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "renderDialog",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );
  const requestCode = readFileSync(join(outRoot, "static", "app", "modules", "ui", "ai", "tool_approval", "request.js"), "utf8");
  const dialogCode = readFileSync(join(outRoot, "static", "app", "modules", "ui", "ai", "tool_approval", "dialog.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");
  assert.match(requestCode, /\btoolApprovalButtonLabel\b/);
  assert.match(requestCode, /\bToolApprovalRequest\b/);
  assert.match(dialogCode, /import \{ ToolApprovalRequest \} from "\.\/request\.js";/);
  assert.doesNotMatch(dialogCode, /import \{ .* as .* \} from "\.\/request\.js";/);
  assert.match(dialogCode, /\brenderToolApprovalDialog\b/);
  assert.match(entryCode, /import \{ renderToolApprovalDialog, __dt_generated_init__ui_ai_tool_approval_dialog \} from "\.\/modules\/ui\/ai\/tool_approval\/dialog\.js";/);
  assert.doesNotMatch(entryCode, /import \{ .* as .* \} from "\.\/modules\/ui\/ai\/tool_approval\/dialog\.js";/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules emits plain-import pure class/function modules without init wrappers", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-plain-import-class-pipeline-"
  );
  const source = `class PureBox {
  static label() {
    return "pure-box";
  }
}
function renderPureBox() {
  return PureBox.label();
}
export { renderPureBox };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const result = await runTransformSpecObject({
    kind: "js.ast_transform_spec",
    operations: [
      {
        id: "logical__pure_box",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "ui/pure_box",
        },
        members: [
          {
            id: "member__pure_box",
            name: "FriendlyPureBox",
            selector: {
              binding: {
                kind: "ClassDeclaration",
                name: "PureBox",
              },
            },
          },
          {
            id: "member__render_pure_box",
            name: "renderFriendlyPureBox",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "renderPureBox",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pipeline: [
      {
        id: "load",
        operation: "load_js_chunks",
        args: {
          inputRoot: snapshotRoot,
          jsListPath: join(extractedRoot, "js-files.txt"),
        },
      },
      {
        id: "asts",
        operation: "compute_js_asts",
      },
      {
        id: "normalize",
        operation: "normalize_js_chunks",
        args: {
          jobs: 1,
        },
      },
      {
        id: "logical",
        operation: "materialize_logical_modules",
        args: {
          chunkIds: ["static/app"],
          pruneOtherChunks: false,
        },
      },
      {
        id: "write",
        operation: "write_js_tree",
        args: {
          force: true,
          outDir: outRoot,
        },
      },
    ],
  });

  assert.deepEqual(
    result.steps.map((step) => step.operation),
    ["load_js_chunks", "compute_js_asts", "normalize_js_chunks", "materialize_logical_modules", "write_js_tree"]
  );
  const pureBoxCode = readFileSync(join(outRoot, "static", "app", "modules", "ui", "pure_box.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");
  assert.match(pureBoxCode, /\bclass FriendlyPureBox\b/);
  assert.match(pureBoxCode, /\bfunction renderFriendlyPureBox\b/);
  assert.doesNotMatch(pureBoxCode, /export function __dt_generated_init__/);
  assert.doesNotMatch(pureBoxCode, /\bPureBox = class PureBox\b/);
  assert.match(pureBoxCode, /export \{ renderFriendlyPureBox \};/);
  assert.doesNotMatch(pureBoxCode, /export \{[^\n}]*\bFriendlyPureBox\b/);
  assert.match(entryCode, /import \{ renderFriendlyPureBox \} from "\.\/modules\/ui\/pure_box\.js";/);
  assert.doesNotMatch(entryCode, /import \{[^}]*\bFriendlyPureBox\b[^}]*\} from "\.\/modules\/ui\/pure_box\.js";/);
  assert.doesNotMatch(entryCode, /__dt_generated_init__ui_pure_box/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules derives logical claimability from the owner graph instead of the preselected owner base", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-entry-claimability-pipeline-"
  );
  const source = `const independentValue = "independent";
function readIndependentValue() {
  return independentValue;
}

console.log("entry-barrier");

const focusLabel = "focus";
class FocusService {
  static label() {
    return focusLabel;
  }
}
function useFocusService() {
  return FocusService.label();
}

console.log(readIndependentValue());
console.log(useFocusService());

export { readIndependentValue, useFocusService };
`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const loaded = loadJsChunks({
    inputRoot: snapshotRoot,
    jsListPath: join(extractedRoot, "js-files.txt"),
  });
  const parsed = computeJsAsts({
    artifact: loaded.artifact,
  });
  const normalized = await normalizeJsChunks({
    artifact: parsed.artifact,
    jobs: 1,
  });
  const runtimeFile = getChunkEntryFile(normalized.artifact, "static/app");
  const analysis = analyzeRuntimeBoundaryAst(runtimeFile.ast, {
    chunkId: "static/app",
    manifestPath: "static/app/manifest.json",
    runtimePath: "static/app/entry.js",
    uiVersion: "fixture",
  });
  const selectedOwnerIdsByChunk = {
    "static/app": ownerIdsForNames(analysis, ["independentValue", "readIndependentValue"]),
  };

  const materialized = materializeLogicalModules({
    artifact: normalized.artifact,
    chunkIds: ["static/app"],
    operations: [
      {
        id: "logical__focus_service",
        operation: "define_logical_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "ui/focus/service",
        },
        members: [
          {
            id: "member__focus_label",
            name: "focusServiceLabel",
            selector: {
              binding: {
                kind: "VariableDeclarator",
                name: "focusLabel",
              },
            },
          },
          {
            id: "member__focus_service",
            name: "FriendlyFocusService",
            selector: {
              binding: {
                kind: "ClassDeclaration",
                name: "FocusService",
              },
            },
          },
          {
            id: "member__use_focus_service",
            name: "useFriendlyFocusService",
            selector: {
              binding: {
                kind: "FunctionDeclaration",
                name: "useFocusService",
              },
            },
          },
        ],
      },
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pruneOtherChunks: false,
    selectedOwnerIdsByChunk,
  });

  writeJsTree({
    artifact: materialized.artifact,
    force: true,
    outDir: outRoot,
  });

  const focusCode = readFileSync(join(outRoot, "static", "app", "modules", "ui", "focus", "service.js"), "utf8");
  const residualCode = readFileSync(join(outRoot, "static", "app", "modules", "residual", "unhandled.js"), "utf8");
  const entryCode = readFileSync(join(outRoot, "static", "app", "entry.js"), "utf8");

  assert.match(withoutGeneratedHeader(focusCode), /Selected-module lowered region; original owners: owner_00002, owner_00003, owner_00004\./);
  assert.match(focusCode, /\bfocusServiceLabel = "focus"/);
  assert.match(focusCode, /^\s*class FriendlyFocusService\b/m);
  assert.match(focusCode, /^\s*function useFriendlyFocusService\(\)/m);
  assert.doesNotMatch(focusCode, /\bFriendlyFocusService = class FriendlyFocusService\b/);
  assert.doesNotMatch(focusCode, /\buseFriendlyFocusService = function useFriendlyFocusService\b/);
  assert.match(focusCode, /__dt_generated_init__ui_focus_service_stage_0/);
  assert.match(focusCode, /__dt_generated_init__ui_focus_service_stage_1/);
  assert.match(focusCode, /export \{ useFriendlyFocusService \};/);
  assert.doesNotMatch(focusCode, /export \{[^\n}]*\bfocusServiceLabel\b/);
  assert.doesNotMatch(focusCode, /export \{[^\n}]*\bFriendlyFocusService\b/);
  assert.match(entryCode, /import \{[^}]*useFriendlyFocusService[^}]*__dt_generated_init__ui_focus_service_stage_0[^}]*__dt_generated_init__ui_focus_service_stage_1[^}]*\} from "\.\/modules\/ui\/focus\/service\.js";/);
  assert.doesNotMatch(entryCode, /import \{[^}]*focusServiceLabel[^}]*\} from "\.\/modules\/ui\/focus\/service\.js";/);
  assert.doesNotMatch(entryCode, /import \{[^}]*\bFriendlyFocusService\b[^}]*\} from "\.\/modules\/ui\/focus\/service\.js";/);
  assert.match(residualCode, /\breadIndependentValue\b/);
  assert.doesNotMatch(residualCode, /\bFriendlyFocusService\b/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

test("materializeLogicalModules closes a supplied selected-owner base over owner dependencies before lowering residual", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-logical-modules-selected-owner-closure-pipeline-"
  );
  const source = `const focusLabel = "focus";
class FocusService {
  static label() {
    return focusLabel;
  }
}
function useFocusService() {
  return FocusService.label();
}

console.log(useFocusService());

export { useFocusService };`;
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const loaded = loadJsChunks({
    inputRoot: snapshotRoot,
    jsListPath: join(extractedRoot, "js-files.txt"),
  });
  const parsed = computeJsAsts({
    artifact: loaded.artifact,
  });
  const normalized = await normalizeJsChunks({
    artifact: parsed.artifact,
    jobs: 1,
  });
  const runtimeFile = getChunkEntryFile(normalized.artifact, "static/app");
  const analysis = analyzeRuntimeBoundaryAst(runtimeFile.ast, {
    chunkId: "static/app",
    manifestPath: "static/app/manifest.json",
    runtimePath: "static/app/entry.js",
    uiVersion: "fixture",
  });
  const selectedOwnerIdsByChunk = {
    "static/app": ownerIdsForNames(analysis, ["useFocusService"]),
  };

  const materialized = materializeLogicalModules({
    artifact: normalized.artifact,
    chunkIds: ["static/app"],
    operations: [
      {
        id: "logical__unhandled",
        operation: "define_residual_module",
        selector: {
          chunkId: "static/app",
        },
        target: {
          path: "residual/unhandled",
        },
      },
    ],
    pruneOtherChunks: false,
    selectedOwnerIdsByChunk,
  });

  writeJsTree({
    artifact: materialized.artifact,
    force: true,
    outDir: outRoot,
  });

  const residualCode = readFileSync(join(outRoot, "static", "app", "modules", "residual", "unhandled.js"), "utf8");
  assert.match(withoutGeneratedHeader(residualCode), /Selected-module lowered region; original owners: owner_00000, owner_00001, owner_00002\./);
  assert.match(residualCode, /\bfocusLabel = "focus"/);
  assert.match(residualCode, /^\s*class FocusService\b/m);
  assert.match(residualCode, /^\s*function useFocusService\(\)/m);
  assert.doesNotMatch(residualCode, /\bFocusService = class FocusService\b/);
  assert.doesNotMatch(residualCode, /\buseFocusService = function useFocusService\b/);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

async function prepareAtomicFixture(prefix) {
  const { extractedRoot, selectedOwnerIds, snapshotRoot } = await writeAtomicSnapshotFixture(prefix);
  const loaded = loadJsChunks({
    inputRoot: snapshotRoot,
    jsListPath: join(extractedRoot, "js-files.txt"),
  });
  const parsed = computeJsAsts({
    artifact: loaded.artifact,
  });
  const normalized = await normalizeJsChunks({
    artifact: parsed.artifact,
    jobs: 1,
  });
  return {
    artifact: normalized.artifact,
    selectedOwnerIds,
    snapshotRoot,
  };
}

async function writeAtomicSnapshotFixture(prefix) {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(prefix);
  const source = fixtureSource();
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const loaded = loadJsChunks({
    inputRoot: snapshotRoot,
    jsListPath: join(extractedRoot, "js-files.txt"),
  });
  const parsed = computeJsAsts({
    artifact: loaded.artifact,
  });
  const normalized = await normalizeJsChunks({
    artifact: parsed.artifact,
    jobs: 1,
  });
  const runtimeFile = getChunkEntryFile(normalized.artifact, "static/app");
  const analysis = analyzeRuntimeBoundaryAst(runtimeFile.ast, {
    chunkId: "static/app",
    manifestPath: "static/app/manifest.json",
    runtimePath: "static/app/entry.js",
    uiVersion: "fixture",
  });
  const selectedOwnerIds = ownerIdsForNames(analysis, ["seed", "readSeed", "first", "readFirst", "second", "render"]);

  return {
    extractedRoot,
    outRoot,
    selectedOwnerIds,
    snapshotRoot,
  };
}

function moduleSelectorForModulePlan(modulePlan) {
  return {
    symbols: [...modulePlan.memberNames],
  };
}

function logicalModuleOpsForFixture() {
  return [
    {
      id: "logical__seed_state",
      operation: "define_logical_module",
      selector: {
        chunkId: "static/app",
      },
      target: {
        path: "state/seed_state",
      },
      members: [
        {
          id: "rename__seed",
          name: "seedValue",
          selector: {
            binding: {
              kind: "VariableDeclarator",
              name: "seed",
            },
          },
        },
        {
          id: "rename__readSeed",
          name: "readSeedValue",
          selector: {
            binding: {
              kind: "FunctionDeclaration",
              name: "readSeed",
            },
          },
        },
      ],
    },
    {
      id: "logical__first_state",
      operation: "define_logical_module",
      selector: {
        chunkId: "static/app",
      },
      target: {
        path: "state/first_state",
      },
      members: [
        {
          id: "rename__first",
          name: "firstValue",
          selector: {
            binding: {
              kind: "VariableDeclarator",
              name: "first",
            },
          },
        },
        {
          id: "rename__readFirst",
          name: "readFirstValue",
          selector: {
            binding: {
              kind: "FunctionDeclaration",
              name: "readFirst",
            },
          },
        },
      ],
    },
    {
      id: "logical__unhandled",
      operation: "define_residual_module",
      selector: {
        chunkId: "static/app",
      },
      target: {
        path: "residual/unhandled",
      },
    },
  ];
}

function ownerIdsForNames(analysis, names) {
  return names.map((name) => {
    const ownerId = analysis.owners.find((owner) => owner.names.includes(name))?.id;
    assert.ok(ownerId, `missing owner ${name}`);
    return ownerId;
  });
}

function readFileSyncFromArtifactFile(fileArtifact) {
  assert.ok(fileArtifact?.ast, "missing artifact file AST");
  return serializeGeneratedJsFile(fileArtifact);
}

function withoutGeneratedHeader(source) {
  const lines = source.split("\n");
  if (lines[0]?.startsWith("// @ducktape-")) {
    return lines.slice(1).join("\n");
  }
  return source;
}

function fixtureSource() {
  const source = `const seed = 1;
function readSeed() {
  return seed;
}

console.log("atomic-barrier-0");

const first = readSeed() + 1;
function readFirst() {
  return first;
}

console.log("atomic-barrier-1");

const second = readFirst() + 1;
function render() {
  return \`second=\${second}\`;
}

console.log(render());

export { first, readFirst, render, second };
`;
  parse(source, DEFAULT_PARSER_OPTIONS);
  analyzeRuntimeBoundaryCode(source, {
    ast: parse(source, DEFAULT_PARSER_OPTIONS),
    chunkId: "static/app",
    runtimePath: "fixture/runtime.js",
    uiVersion: "fixture",
  });
  return source;
}
