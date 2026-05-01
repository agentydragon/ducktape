import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

import { analyzeRuntimeBoundaryCode } from "../analysis/boundary.mjs";
import { packSelectedModuleGroups, planSelectedModuleGroupExtractions } from "../extract/decl_graph.mjs";

const REDUCED_CHUNK = `
const seen = {};
const __vitePreload = function preload(baseModule, deps) {
  if (deps && deps.length > 0) {
    for (const dep of deps) {
      if (dep in seen) return;
      seen[dep] = true;
    }
  }
  return baseModule();
};
await __vitePreload(() => Promise.resolve(), ["x.css"]);
`;

test("tiny repro: reduced chunk still triggers JS/Rust closure mismatch", () => {
  const tmp = mkdtempSync(join(tmpdir(), "debundle-tiny-repro-"));
  const inputRoot = join(tmp, "input");
  const outRoot = join(tmp, "out");
  mkdirSync(join(inputRoot, "static"), { recursive: true });
  const sourcePath = "static/index-TinyRepro.js";
  writeFileSync(join(inputRoot, sourcePath), REDUCED_CHUNK);
  writeFileSync(join(inputRoot, "js-files.txt"), `${sourcePath}\n`);

  const run = spawnSync(resolveRustBin(), ["--input-root", inputRoot, "--js-list", join(inputRoot, "js-files.txt"), "--out-root", outRoot], { encoding: "utf8" });
  assert.equal(run.status, 0, `debundle_rust failed:\n${run.stderr}`);

  const rustPlanner = JSON.parse(readFileSync(join(outRoot, "planner_snapshot.json"), "utf8"));
  const rustFrontier = normalizeFrontier(
    (rustPlanner.debug?.frontierTraces ?? rustPlanner.debug?.frontier_traces ?? []).map((trace) => ({
      seedOwnerIds: trace.seedOwnerIds ?? trace.seed_owner_ids ?? trace.seed_component_owner_ids ?? [],
      seedMemberNames: trace.seedMemberNames ?? trace.seed_member_names ?? [],
      seedComponentDepOwnerIds: trace.seedComponentDepOwnerIds ?? trace.seed_component_dep_owner_ids ?? [],
      requiredClosureOwnerIds: trace.requiredClosureOwnerIds ?? trace.required_closure_owner_ids ?? [],
      requiredComponentIds: trace.requiredComponentIds ?? trace.required_component_ids ?? [],
      contiguousEnvelopeComponentIds: trace.contiguousEnvelopeComponentIds ?? trace.contiguous_envelope_component_ids ?? [],
    }))
  );

  const jsPacked = packSelectedModuleGroups(
    planSelectedModuleGroupExtractions(
      analyzeRuntimeBoundaryCode(REDUCED_CHUNK, {
        chunkId: "static/index-TinyRepro",
        runtimePath: "static/index-TinyRepro/entry.js",
        uiVersion: "planner-parity",
      })
    ),
    { lowering: "staged_shell" }
  );
  const jsFrontier = normalizeFrontier(
    (jsPacked.candidateBatchPlans ?? []).map((batchPlan) => ({
      seedOwnerIds: batchPlan.seedOwnerIds ?? [],
      seedMemberNames: batchPlan.seedMemberNames ?? [],
      seedComponentDepOwnerIds: batchPlan.seedComponentDepOwnerIds ?? [],
      requiredClosureOwnerIds: batchPlan.requiredClosureOwnerIds ?? batchPlan.ownerIds ?? [],
      requiredComponentIds: batchPlan.requiredComponentIds ?? [],
      contiguousEnvelopeComponentIds: batchPlan.contiguousEnvelopeComponentIds ?? [],
    }))
  );

  const firstDivergence = findFirstDivergence(rustFrontier, jsFrontier);

  assert.ok(
    firstDivergence,
    [
      "expected a closure mismatch for reduced chunk, but none found",
      formatStateTable(rustFrontier, jsFrontier),
    ].join("\n\n")
  );

  assert.equal(
    firstDivergence.field,
    "requiredClosureOwnerIds",
    [
      "unexpected first divergence field",
      `field=${firstDivergence.field}`,
      formatStateTable(rustFrontier, jsFrontier),
    ].join("\n\n")
  );
});

function normalizeFrontier(traces) {
  return traces
    .map((trace) => ({
      seed: [...(trace.seedOwnerIds ?? [])].sort().join("|"),
      seedOwnerIds: [...(trace.seedOwnerIds ?? [])].sort(),
      seedMemberNames: [...(trace.seedMemberNames ?? [])].sort(),
      seedComponentDepOwnerIds: [...(trace.seedComponentDepOwnerIds ?? [])].sort(),
      requiredClosureOwnerIds: [...(trace.requiredClosureOwnerIds ?? [])].sort(),
      requiredComponentIds: [...(trace.requiredComponentIds ?? [])].sort(),
      contiguousEnvelopeComponentIds: [...(trace.contiguousEnvelopeComponentIds ?? [])].sort(),
    }))
    .sort((left, right) => left.seed.localeCompare(right.seed));
}

function findFirstDivergence(rustFrontier, jsFrontier) {
  const jsBySeed = new Map(jsFrontier.map((row) => [row.seed, row]));
  for (const rust of rustFrontier) {
    const js = jsBySeed.get(rust.seed);
    if (!js) {
      return { seed: rust.seed, field: "missingSeedInJs" };
    }
    for (const field of [
      "seedMemberNames",
      "seedComponentDepOwnerIds",
      "requiredComponentIds",
      "contiguousEnvelopeComponentIds",
      "requiredClosureOwnerIds",
    ]) {
      if (JSON.stringify(rust[field]) !== JSON.stringify(js[field])) {
        return { seed: rust.seed, field };
      }
    }
  }
  return null;
}

function formatStateTable(rustFrontier, jsFrontier) {
  const headers = ["seed", "field", "rust", "js", "equal"];
  const rows = [headers];
  const fields = [
    "seedMemberNames",
    "seedComponentDepOwnerIds",
    "requiredComponentIds",
    "contiguousEnvelopeComponentIds",
    "requiredClosureOwnerIds",
  ];
  const jsBySeed = new Map(jsFrontier.map((row) => [row.seed, row]));
  for (const rust of rustFrontier) {
    const js = jsBySeed.get(rust.seed);
    for (const field of fields) {
      const left = JSON.stringify(rust[field]);
      const right = JSON.stringify(js?.[field] ?? null);
      rows.push([rust.seed, field, left, right, left === right ? "yes" : "no"]);
    }
  }
  return rows.map((row) => row.join("\t")).join("\n");
}

function resolveRustBin() {
  const runfiles = process.env.TEST_SRCDIR;
  const workspace = process.env.TEST_WORKSPACE;
  const rel = process.env.RUST_DEBUNDLE_BIN;
  assert.ok(runfiles && workspace && rel, "bazel runfiles env for rust bin missing");
  return join(runfiles, workspace, rel);
}
