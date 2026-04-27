import { readFileSync } from "node:fs";
import { basename, resolve } from "node:path";
import { packOrderedInitOwnerClosures, planOrderedInitOwnerClosureExtractions } from "../../devinfra/js/debundle/extract/decl_graph.mjs";

function main() {
  const analysisPath = process.argv[2];
  if (!analysisPath) {
    throw new Error("usage: node x/benchmarks/ordered_init_owner_closure_packing_benchmark.mjs <boundary-analysis.json>");
  }

  const resolvedAnalysisPath = resolve(analysisPath);
  const analysis = JSON.parse(readFileSync(resolvedAnalysisPath, "utf8"));
  const planStartedAt = process.hrtime.bigint();
  const plan = planOrderedInitOwnerClosureExtractions(analysis);
  const planMs = durationMsSince(planStartedAt);
  const packStartedAt = process.hrtime.bigint();
  const packed = packOrderedInitOwnerClosures(plan, { lowering: "staged_shell" });
  const packMs = durationMsSince(packStartedAt);

  console.log(
    JSON.stringify(
      {
        analysis: basename(resolvedAnalysisPath),
        batchPlans: packed.batchPlans.length,
        candidateBatchPlans: packed.candidateBatchPlans.length,
        closurePlans: plan.closurePlans.length,
        packMs,
        packTimingsMs: packed.timingsMs ?? null,
        planMs,
        totalMs: planMs + packMs,
      },
      null,
      2
    )
  );
}

function durationMsSince(startedAt) {
  return Number(process.hrtime.bigint() - startedAt) / 1_000_000;
}

main();
