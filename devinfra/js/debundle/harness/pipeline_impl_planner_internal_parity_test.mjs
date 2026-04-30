import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { analyzeRuntimeBoundaryCode } from "../analysis/boundary.mjs";
import { getOrderedInitPlannerStateForTesting, packSelectedModuleGroups, planSelectedModuleGroupExtractions } from "../extract/decl_graph.mjs";
import { buildJsGolden, buildRustGolden } from "./pipeline_impl_golden_lib.mjs";

test("planner internals parity: rust extraction groups exactly match js planner batch groups", async () => {
  const tmp = mkdtempSync(join(tmpdir(), "debundle-planner-internal-parity-"));
  const jsOut = join(tmp, "js");
  const rustOut = join(tmp, "rust");
  await buildJsGolden(jsOut);
  buildRustGolden(rustOut, resolveRustBin());

  const chunkManifest = JSON.parse(readFileSync(join(jsOut, "chunks.manifest.json"), "utf8"));
  const jsExtractionGroups = [];
  const jsCandidatesDebug = [];
  const jsSelectedDebug = [];
  const jsOrderedInitStates = [];
  for (const chunk of chunkManifest.chunks) {
    const code = readFileSync(join(jsOut, chunk.chunkId, "entry.js"), "utf8");
    const analysis = analyzeRuntimeBoundaryCode(code, {
      chunkId: chunk.chunkId,
      runtimePath: `${chunk.chunkId}/entry.js`,
      uiVersion: "planner-parity",
    });
    const plan = planSelectedModuleGroupExtractions(analysis);
    const packed = packSelectedModuleGroups(plan, { lowering: "staged_shell" });
    jsOrderedInitStates.push(normalizeOrderedInitState(getOrderedInitPlannerStateForTesting(analysis)));
    for (const batchPlan of packed.candidateBatchPlans ?? []) {
      jsCandidatesDebug.push({
        ownerIds: [...batchPlan.semanticOwnerIds].sort(),
        memberNames: [...batchPlan.semanticMemberNames].sort(),
        estimatedSize: Number(batchPlan.estimatedSize ?? 0),
        shellItemIds: [...(batchPlan.shellItemIds ?? [])].sort(),
        semanticBlockingReasons: [...(batchPlan.semanticBlockingReasons ?? [])].sort(),
        stageRuns: normalizeStageRuns(batchPlan.stageRuns ?? []),
      });
    }
    for (const batchPlan of packed.batchPlans) {
      const members = [...batchPlan.semanticMemberNames].sort();
      jsExtractionGroups.push(members);
      jsSelectedDebug.push({
        ownerIds: [...batchPlan.semanticOwnerIds].sort(),
        memberNames: members,
        estimatedSize: Number(batchPlan.estimatedSize ?? members.length),
        shellItemIds: [...(batchPlan.shellItemIds ?? [])].sort(),
        semanticBlockingReasons: [...(batchPlan.semanticBlockingReasons ?? [])].sort(),
        stageRuns: normalizeStageRuns(batchPlan.stageRuns ?? []),
      });
    }
  }

  const rustPlanner = JSON.parse(readFileSync(join(rustOut, "planner_snapshot.json"), "utf8"));
  assert.deepEqual(
    normalizeGroups(rustPlanner.extractionGroups),
    normalizeGroups(jsExtractionGroups),
    "rust extraction groups diverged from js planner semantic owner groups"
  );

  assert.deepEqual(
    normalizeSelectedDebug(rustPlanner.debug?.candidates ?? []),
    normalizeSelectedDebug(jsCandidatesDebug),
    "rust candidate debug diverged from js candidate batch plans"
  );

  assert.deepEqual(
    normalizeSelectedDebug(rustPlanner.debug?.selected ?? []),
    normalizeSelectedDebug(jsSelectedDebug),
    "rust selected candidate debug diverged from js packed planner output"
  );
  assert.deepEqual(
    normalizeOrderedInitState(rustPlanner.debug?.orderedInitState ?? {}),
    mergeOrderedInitStates(jsOrderedInitStates),
    "rust ordered-init planner-state mappings diverged from js planner state"
  );
});

function resolveRustBin() {
  const runfiles = process.env.TEST_SRCDIR;
  const workspace = process.env.TEST_WORKSPACE;
  const rel = process.env.RUST_DEBUNDLE_BIN;
  assert.ok(runfiles && workspace && rel, "bazel runfiles env for rust bin missing");
  return join(runfiles, workspace, rel);
}


function normalizeGroups(groups) {
  return groups
    .map((group) => [...group].sort())
    .sort((left, right) => left.join("\n").localeCompare(right.join("\n")));
}

function normalizeSelectedDebug(records) {
  return records
    .map((record) => ({
      ownerIds: [...(record.ownerIds ?? record.owner_ids ?? [])].sort(),
      memberNames: [...(record.memberNames ?? record.member_names ?? [])].sort(),
      estimatedSize: Number(record.estimatedSize ?? record.estimated_size ?? 0),
      shellItemIds: [...(record.shellItemIds ?? record.shell_item_ids ?? [])].sort(),
      semanticBlockingReasons: [
        ...(record.semanticBlockingReasons ?? record.semantic_blocking_reasons ?? []),
      ].sort(),
      stageRuns: normalizeStageRuns(record.stageRuns ?? record.stage_runs ?? []),
    }))
    .sort((left, right) => left.memberNames.join("\n").localeCompare(right.memberNames.join("\n")));
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
  const normalizedByOwner = Object.fromEntries(
    Object.entries(byOwner).map(([ownerId, ids]) => [ownerId, [...ids].sort()]).sort(([a],[b]) => a.localeCompare(b))
  );
  const normalizedById = Object.fromEntries(
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
  );
  return { replayableSideEffectIdsByOwnerId: normalizedByOwner, replayableSideEffectStateById: normalizedById };
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
