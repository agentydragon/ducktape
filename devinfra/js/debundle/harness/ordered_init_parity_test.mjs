import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { analyzeRuntimeBoundaryCode } from "../analysis/boundary.mjs";
import { getOrderedInitPlannerStateForTesting } from "../extract/decl_graph.mjs";
import { buildJsGolden, buildRustGolden } from "./pipeline_impl_golden_lib.mjs";

test("ordered-init planner-state maps: rust exactly matches js", async () => {
  const tmp = mkdtempSync(join(tmpdir(), "debundle-ordered-init-parity-"));
  const jsOut = join(tmp, "js");
  const rustOut = join(tmp, "rust");
  await buildJsGolden(jsOut);
  buildRustGolden(rustOut, resolveRustBin());

  const chunkManifest = JSON.parse(readFileSync(join(jsOut, "chunks.manifest.json"), "utf8"));
  const jsStates = [];
  for (const chunk of chunkManifest.chunks) {
    const code = readFileSync(join(jsOut, chunk.chunkId, "entry.js"), "utf8");
    const analysis = analyzeRuntimeBoundaryCode(code, {
      chunkId: chunk.chunkId,
      runtimePath: `${chunk.chunkId}/entry.js`,
      uiVersion: "planner-parity",
    });
    jsStates.push(normalizeOrderedInitState(getOrderedInitPlannerStateForTesting(analysis)));
  }

  const rustPlanner = JSON.parse(readFileSync(join(rustOut, "planner_snapshot.json"), "utf8"));
  assert.deepEqual(
    normalizeOrderedInitState(rustPlanner.debug?.orderedInitState ?? {}),
    mergeOrderedInitStates(jsStates),
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

function normalizeOrderedInitState(state) {
  const byOwner = state.replayableSideEffectIdsByOwnerId ?? state.replayable_side_effect_ids_by_owner_id ?? {};
  const byId = state.replayableSideEffectStateById ?? state.replayable_side_effect_state_by_id ?? {};
  return {
    replayableSideEffectIdsByOwnerId: Object.fromEntries(
      Object.entries(byOwner).map(([ownerId, ids]) => [ownerId, [...ids].sort()]).sort(([a], [b]) => a.localeCompare(b))
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
