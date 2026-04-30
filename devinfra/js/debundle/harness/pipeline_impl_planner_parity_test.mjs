import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { analyzeRuntimeBoundaryCode } from "../analysis/boundary.mjs";
import { getOrderedInitPlannerStateForTesting, packSelectedModuleGroups, planSelectedModuleGroupExtractions } from "../extract/decl_graph.mjs";
import { buildJsGolden, buildRustGolden } from "./pipeline_impl_golden_lib.mjs";

test("rust planner snapshot matches js-derived snapshot on mock fixture", async () => {
  const tmp = mkdtempSync(join(tmpdir(), "debundle-planner-parity-"));
  const jsOut = join(tmp, "js");
  const rustOut = join(tmp, "rust");
  await buildJsGolden(jsOut);
  buildRustGolden(rustOut, resolveRustBin());

  const jsSnapshot = buildJsSnapshot(jsOut);
  const rustSnapshot = JSON.parse(readFileSync(join(rustOut, "planner_snapshot.json"), "utf8"));
  assert.deepEqual(normalize(jsSnapshot), normalize(rustSnapshot));
});

function resolveRustBin() {
  const runfiles = process.env.TEST_SRCDIR;
  const workspace = process.env.TEST_WORKSPACE;
  const rel = process.env.RUST_DEBUNDLE_BIN;
  assert.ok(runfiles && workspace && rel, "bazel runfiles env for rust bin missing");
  return join(runfiles, workspace, rel);
}

function buildJsSnapshot(jsRoot) {
  const chunkManifest = JSON.parse(readFileSync(join(jsRoot, "chunks.manifest.json"), "utf8"));
  const modules = chunkManifest.chunks.map((c) => {
    const code = readFileSync(join(jsRoot, c.chunkId, "entry.js"), "utf8");
    const imports = [...code.matchAll(/import\s+["']\.\/([^"']+)["']/g)].map((m) =>
      m[1].replace(/\/entry\.js$/, ".js")
    );
    return {
      id: c.sourcePath,
      imports: imports.sort(),
      hasTopLevelEffects: code.includes("new ") || code.includes("window.") || code.includes("document."),
    };
  });
  const selectedModules = modules.map((m) => m.id).sort();
  const { extractionGroups, debug } = deriveExtractionGroupsFromJsPlanner(jsRoot, chunkManifest);
  return {
    schemaVersion: 1,
    modules,
    selectedModules,
    extractionGroups,
    rationale: "selected-owner closure planning over dependency components with side-effect order constraints",
    debug,
  };
}

function deriveExtractionGroupsFromJsPlanner(jsRoot, chunkManifest) {
  const groups = [];
  const selected = [];
  const orderedInitStates = [];
  for (const chunk of chunkManifest.chunks) {
    const code = readFileSync(join(jsRoot, chunk.chunkId, "entry.js"), "utf8");
    const analysis = analyzeRuntimeBoundaryCode(code, {
      chunkId: chunk.chunkId,
      runtimePath: `${chunk.chunkId}/entry.js`,
      uiVersion: "planner-parity",
    });
    const plan = planSelectedModuleGroupExtractions(analysis);
    const packed = packSelectedModuleGroups(plan, { lowering: "staged_shell" });
    orderedInitStates.push(normalizeOrderedInitState(getOrderedInitPlannerStateForTesting(analysis)));
    for (const batchPlan of packed.batchPlans) {
      const members = [...batchPlan.semanticMemberNames].sort();
      groups.push(members);
      selected.push({
        ownerIds: [...batchPlan.semanticOwnerIds].sort(),
        memberNames: members,
        estimatedSize: Number(batchPlan.estimatedSize ?? members.length),
        shellItemIds: [...(batchPlan.shellItemIds ?? [])].sort(),
        semanticBlockingReasons: [...(batchPlan.semanticBlockingReasons ?? [])].sort(),
        stageRuns: normalizeStageRuns(batchPlan.stageRuns ?? []),
      });
    }
  }
  return {
    extractionGroups: groups.sort((a, b) => a.join("\n").localeCompare(b.join("\n"))),
    debug: {
      selected,
      orderedInitState: mergeOrderedInitStates(orderedInitStates),
    },
  };
}

function normalize(snapshot) {
  const modules = [...snapshot.modules]
    .map((m) => ({
      id: m.id,
      imports: [...m.imports].sort(),
      hasTopLevelEffects: Boolean(m.hasTopLevelEffects),
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
  const selectedModules = [...snapshot.selectedModules].sort();
  const extractionGroups = snapshot.extractionGroups
    .map((g) => [...g].sort())
    .sort((a, b) => a[0].localeCompare(b[0]));
  return {
    schemaVersion: 1,
    modules,
    selectedModules,
    extractionGroups,
    rationale: snapshot.rationale,
    debug: {
      selected: (snapshot.debug?.selected ?? [])
        .map((s) => ({
          ownerIds: [...(s.ownerIds ?? [])].sort(),
          memberNames: [...(s.memberNames ?? [])].sort(),
          estimatedSize: Number(s.estimatedSize ?? 0),
          shellItemIds: [...(s.shellItemIds ?? s.shell_item_ids ?? [])].sort(),
          semanticBlockingReasons: [
            ...(s.semanticBlockingReasons ?? s.semantic_blocking_reasons ?? []),
          ].sort(),
          stageRuns: normalizeStageRuns(s.stageRuns ?? s.stage_runs ?? []),
        }))
        .sort((a, b) => a.memberNames.join("\n").localeCompare(b.memberNames.join("\n"))),
      orderedInitState: normalizeOrderedInitState(snapshot.debug?.orderedInitState ?? snapshot.debug?.ordered_init_state ?? {}),
    },
  };
}

function normalizeStageRuns(stageRuns) {
  return [...stageRuns]
    .map((stage) => ({
      id: stage.id,
      startOrdinal: Number(stage.startOrdinal ?? stage.start_ordinal ?? 0),
      endOrdinal: Number(stage.endOrdinal ?? stage.end_ordinal ?? 0),
      itemIds: [...(stage.itemIds ?? stage.item_ids ?? [])].sort(),
      ownerIds: [...(stage.ownerIds ?? stage.owner_ids ?? [])].sort(),
      memberNames: [...(stage.memberNames ?? stage.member_names ?? [])].sort(),
    }))
    .sort(
      (left, right) =>
        left.startOrdinal - right.startOrdinal || left.id.localeCompare(right.id)
    );
}

function normalizeOrderedInitState(state) {
  const byOwner = state.replayableSideEffectIdsByOwnerId ?? state.replayable_side_effect_ids_by_owner_id ?? {};
  const byId = state.replayableSideEffectStateById ?? state.replayable_side_effect_state_by_id ?? {};
  return {
    replayableSideEffectIdsByOwnerId: Object.fromEntries(
      Object.entries(byOwner).map(([ownerId, ids]) => [ownerId, [...ids].sort()]).sort(([a],[b]) => a.localeCompare(b))
    ),
    replayableSideEffectStateById: Object.fromEntries(
      Object.entries(byId)
        .map(([id, record]) => [
          id,
          {
            id: record.id,
            runtimeSensitive: Boolean(record.runtimeSensitive ?? record.runtime_sensitive),
            touchedOwnerIds: [...(record.touchedOwnerIds ?? record.touched_owner_ids ?? [])].sort(),
          },
        ])
        .sort(([a], [b]) => a.localeCompare(b))
    ),
  };
}

function mergeOrderedInitStates(states) {
  const mergedByOwner = {};
  const mergedById = {};
  for (const state of states) {
    for (const [ownerId, ids] of Object.entries(state.replayableSideEffectIdsByOwnerId)) {
      mergedByOwner[ownerId] = [...new Set([...(mergedByOwner[ownerId] ?? []), ...ids])].sort();
    }
    for (const [id, record] of Object.entries(state.replayableSideEffectStateById)) {
      mergedById[id] = record;
    }
  }
  return normalizeOrderedInitState({
    replayableSideEffectIdsByOwnerId: mergedByOwner,
    replayableSideEffectStateById: mergedById,
  });
}
