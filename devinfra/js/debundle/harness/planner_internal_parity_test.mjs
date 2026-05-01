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
  const jsFrontierDebug = [];
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
      jsFrontierDebug.push({
        seedComponentId: batchPlan.seedComponentId ?? null,
        seedOwnerIds: [...(batchPlan.seedOwnerIds ?? [])].sort(),
        seedMemberNames: [...(batchPlan.seedMemberNames ?? [])].sort(),
        seedComponentDepOwnerIds: [...(batchPlan.seedComponentDepOwnerIds ?? [])].sort(),
        requiredComponentIds: [
          ...(batchPlan.requiredClosureComponentIds ?? batchPlan.closureComponentIds ?? []),
        ].sort(),
        requiredClosureOwnerIds: [...(batchPlan.requiredClosureOwnerIds ?? batchPlan.ownerIds ?? [])].sort(),
        contiguousEnvelopeComponentIds: [...(batchPlan.contiguousEnvelopeComponentIds ?? [])].sort(),
        closureOwnerIds: [...(batchPlan.ownerIds ?? [])].sort(),
        envelopeStartOrdinal: Number(batchPlan.contiguousEnvelopeStartOrdinal ?? 0),
        envelopeEndOrdinal: Number(batchPlan.contiguousEnvelopeEndOrdinal ?? 0),
        envelopeBarrierItemIds: [...(batchPlan.contiguousEnvelopeBlockingReasons ?? [])]
          .filter((reason) => reason.startsWith("non_declaration_in_envelope:"))
          .sort(),
      });
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
  const rustFrontierSummary = (rustPlanner.debug?.frontierTraces ?? rustPlanner.debug?.frontier_traces ?? [])
    .map((trace) => ({
      seedComponentId: trace.seedComponentId ?? trace.seed_component_id,
      seedOwnerIds: trace.seedOwnerIds ?? trace.seed_owner_ids ?? trace.seedComponentOwnerIds ?? trace.seed_component_owner_ids ?? [],
      seedMemberNames:
        trace.seedMemberNames ?? trace.seed_member_names ?? trace.seedComponentMemberNames ?? trace.seed_component_member_names ?? [],
      seedComponentDepOwnerIds:
        trace.seedComponentDepOwnerIds ?? trace.seed_component_dep_owner_ids ?? [],
      requiredComponentIds: trace.requiredComponentIds ?? trace.required_component_ids ?? [],
      requiredClosureOwnerIds:
        trace.requiredClosureOwnerIds ?? trace.required_closure_owner_ids ?? [],
      contiguousEnvelopeComponentIds:
        trace.contiguousEnvelopeComponentIds ?? trace.contiguous_envelope_component_ids ?? [],
      closureOwnerIds: trace.closureOwnerIds ?? trace.closure_owner_ids ?? [],
      envelopeStartOrdinal: Number(trace.envelopeStartOrdinal ?? trace.envelope_start_ordinal ?? 0),
      envelopeEndOrdinal: Number(trace.envelopeEndOrdinal ?? trace.envelope_end_ordinal ?? 0),
      envelopeBarrierItemIds: trace.envelopeBarrierItemIds ?? trace.envelope_barrier_item_ids ?? [],
    }));
  const normalizedJsFrontier = normalizeFrontierTraces(jsFrontierDebug);
  const normalizedRustFrontier = normalizeFrontierTraces(rustFrontierSummary);
  for (const gate of [
    { label: "requiredClosureOwnerIds", projector: (trace) => trace.requiredClosureOwnerIds },
    { label: "contiguousEnvelopeComponentIds", projector: (trace) => trace.contiguousEnvelopeComponentIds },
    { label: "closureOwnerIds", projector: (trace) => trace.closureOwnerIds },
  ]) {
    const firstGateDelta = firstFrontierGateDelta(normalizedRustFrontier, normalizedJsFrontier, gate.projector);
    if (firstGateDelta) {
      const rustTraceAtDelta = normalizedRustFrontier.find(
        (trace) => trace.seedOwnerIds.join("|") === firstGateDelta.seed
      );
      const jsTraceAtDelta = normalizedJsFrontier.find(
        (trace) => trace.seedOwnerIds.join("|") === firstGateDelta.seed
      );
      assert.fail(
        [
          `frontier layer gate failed: ${gate.label}`,
          `first mismatch seed: ${firstGateDelta.seed}`,
          `rust: ${JSON.stringify(firstGateDelta.rust, null, 2)}`,
          `js: ${JSON.stringify(firstGateDelta.js, null, 2)}`,
          `rust trace@delta: ${JSON.stringify(rustTraceAtDelta, null, 2)}`,
          `js trace@delta: ${JSON.stringify(jsTraceAtDelta, null, 2)}`,
          `rust owner analyses for seed: ${JSON.stringify(
            ownerAnalysesForSeed(rustPlanner.debug?.ownerAnalyses ?? rustPlanner.debug?.owner_analyses ?? [], firstGateDelta.seed),
            null,
            2
          )}`,
        ].join("\n")
      );
    }
  }
  const normalizedRustCandidates = normalizeCandidateUniverse(rustPlanner.debug?.candidates ?? []);
  const normalizedJsCandidates = normalizeCandidateUniverse(jsCandidatesDebug);
  const firstCandidateDelta = firstCandidateUniverseDelta(normalizedRustCandidates, normalizedJsCandidates);
  if (firstCandidateDelta) {
    const firstFrontierDelta = firstCandidateUniverseDelta(normalizedRustFrontier, normalizedJsFrontier);
    assert.fail(
      [
        "rust pre-packing candidate universe diverged from js candidate universe",
        `first mismatch index: ${firstCandidateDelta.index}`,
        `rust candidate: ${JSON.stringify(firstCandidateDelta.rust, null, 2)}`,
        `js candidate: ${JSON.stringify(firstCandidateDelta.js, null, 2)}`,
        `first frontier mismatch: ${JSON.stringify(firstFrontierDelta, null, 2)}`,
        `rust frontier traces (first 5): ${JSON.stringify(normalizedRustFrontier.slice(0, 5), null, 2)}`,
        `js frontier traces (first 5): ${JSON.stringify(normalizedJsFrontier.slice(0, 5), null, 2)}`,
      ].join("\n")
    );
  }
  assert.deepEqual(
    normalizedRustCandidates,
    normalizedJsCandidates,
    "rust pre-packing candidate universe diverged from js candidate universe"
  );
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

function ownerAnalysesForSeed(ownerAnalyses, seed) {
  const seedOwnerIds = seed.split("|").filter(Boolean);
  const related = new Set(seedOwnerIds);
  const byId = new Map(ownerAnalyses.map((owner) => [owner.id, owner]));
  for (const ownerId of seedOwnerIds) {
    const owner = byId.get(ownerId);
    if (!owner) continue;
    for (const depOwnerId of owner.depOwnerIds ?? owner.dep_owner_ids ?? []) {
      related.add(depOwnerId);
    }
    for (const depOwnerId of owner.eagerDepOwnerIds ?? owner.eager_dep_owner_ids ?? []) {
      related.add(depOwnerId);
    }
  }
  return ownerAnalyses
    .filter((owner) => related.has(owner.id))
    .map((owner) => ({
      id: owner.id,
      moduleId: owner.moduleId ?? owner.module_id,
      memberName: owner.memberName ?? owner.member_name,
      depOwnerIds: [...(owner.depOwnerIds ?? owner.dep_owner_ids ?? [])].sort(),
      eagerDepOwnerIds: [...(owner.eagerDepOwnerIds ?? owner.eager_dep_owner_ids ?? [])].sort(),
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
}

function firstCandidateUniverseDelta(rust, js) {
  const max = Math.max(rust.length, js.length);
  for (let idx = 0; idx < max; idx++) {
    const left = rust[idx] ?? null;
    const right = js[idx] ?? null;
    if (JSON.stringify(left) !== JSON.stringify(right)) {
      return { index: idx, rust: left, js: right };
    }
  }
  return null;
}

function firstFrontierGateDelta(rustFrontier, jsFrontier, projector) {
  const rustBySeed = new Map(
    rustFrontier.map((trace) => [trace.seedOwnerIds.join("|"), [...projector(trace)].sort()])
  );
  const jsBySeed = new Map(
    jsFrontier.map((trace) => [trace.seedOwnerIds.join("|"), [...projector(trace)].sort()])
  );
  const seeds = [...new Set([...rustBySeed.keys(), ...jsBySeed.keys()])].sort();
  for (const seed of seeds) {
    const rustValues = rustBySeed.get(seed) ?? null;
    const jsValues = jsBySeed.get(seed) ?? null;
    if (JSON.stringify(rustValues) !== JSON.stringify(jsValues)) {
      return {
        seed,
        rust: { seedComponentId: seed, values: rustValues },
        js: { seedComponentId: seed, values: jsValues },
      };
    }
  }
  return null;
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

function normalizeCandidateUniverse(records) {
  return records
    .map((record) => ({
      ownerIds: [...(record.ownerIds ?? record.owner_ids ?? [])].sort(),
      memberNames: [...(record.memberNames ?? record.member_names ?? [])].sort(),
      estimatedSize: Number(record.estimatedSize ?? record.estimated_size ?? 0),
      semanticBlockingReasons: [
        ...(record.semanticBlockingReasons ?? record.semantic_blocking_reasons ?? []),
      ].sort(),
    }))
    .sort((left, right) =>
      left.ownerIds.join("\n").localeCompare(right.ownerIds.join("\n")) ||
      left.memberNames.join("\n").localeCompare(right.memberNames.join("\n"))
    );
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

function normalizeFrontierTraces(traces) {
  const normalized = traces
    .map((trace) => ({
      seedComponentId: trace.seedComponentId ?? null,
      seedOwnerIds: [...(trace.seedOwnerIds ?? [])].sort(),
      seedMemberNames: [...(trace.seedMemberNames ?? [])].sort(),
      seedComponentDepOwnerIds: [...(trace.seedComponentDepOwnerIds ?? [])].sort(),
      requiredComponentIds: [...(trace.requiredComponentIds ?? [])].sort(),
      requiredClosureOwnerIds: [...(trace.requiredClosureOwnerIds ?? [])].sort(),
      contiguousEnvelopeComponentIds: [...(trace.contiguousEnvelopeComponentIds ?? [])].sort(),
      closureOwnerIds: [...(trace.closureOwnerIds ?? [])].sort(),
      envelopeStartOrdinal: Number(trace.envelopeStartOrdinal ?? 0),
      envelopeEndOrdinal: Number(trace.envelopeEndOrdinal ?? 0),
      envelopeBarrierItemIds: [...(trace.envelopeBarrierItemIds ?? [])].sort(),
    }))
    .sort((left, right) =>
      left.seedOwnerIds.join("\n").localeCompare(right.seedOwnerIds.join("\n")) ||
      (left.seedComponentId ?? "").localeCompare(right.seedComponentId ?? "")
    );
  const deduped = [];
  const seen = new Set();
  for (const record of normalized) {
    const key = JSON.stringify(record);
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    deduped.push(record);
  }
  return deduped;
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
