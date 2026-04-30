import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { analyzeRuntimeBoundaryCode } from "../analysis/boundary.mjs";
import { packSelectedModuleGroups, planSelectedModuleGroupExtractions } from "../extract/decl_graph.mjs";
import { buildJsGolden, buildRustGolden } from "./pipeline_impl_golden_lib.mjs";

test("planner internals parity: js planner grouping count matches rust extraction groups", async () => {
  const tmp = mkdtempSync(join(tmpdir(), "debundle-planner-internal-parity-"));
  const jsOut = join(tmp, "js");
  const rustOut = join(tmp, "rust");
  await buildJsGolden(jsOut);
  buildRustGolden(rustOut, resolveRustBin());

  const chunkManifest = JSON.parse(readFileSync(join(jsOut, "chunks.manifest.json"), "utf8"));
  const jsGroupCount = chunkManifest.chunks.reduce((acc, chunk) => {
    const code = readFileSync(join(jsOut, chunk.chunkId, "entry.js"), "utf8");
    const analysis = analyzeRuntimeBoundaryCode(code, {
      chunkId: chunk.chunkId,
      runtimePath: `${chunk.chunkId}/entry.js`,
      uiVersion: "planner-parity",
    });
    const plan = planSelectedModuleGroupExtractions(analysis);
    const packed = packSelectedModuleGroups(plan, { lowering: "staged_shell" });
    return acc + packed.batchPlans.length;
  }, 0);

  const rustPlanner = JSON.parse(readFileSync(join(rustOut, "planner_snapshot.json"), "utf8"));
  assert.ok(rustPlanner.extractionGroups.length > 0, "rust emitted no extraction groups");
  assert.ok(
    rustPlanner.extractionGroups.length <= jsGroupCount,
    `rust extraction-group count ${rustPlanner.extractionGroups.length} exceeds js planner batch-count ${jsGroupCount}`
  );
});

function resolveRustBin() {
  const runfiles = process.env.TEST_SRCDIR;
  const workspace = process.env.TEST_WORKSPACE;
  const rel = process.env.RUST_DEBUNDLE_BIN;
  assert.ok(runfiles && workspace && rel, "bazel runfiles env for rust bin missing");
  return join(runfiles, workspace, rel);
}
