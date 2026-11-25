// Certify the same owner assignment used for diagnostic comparison, from the JS bytes.
import path from "node:path";
import { writeDeployment } from "../translation/testing/deployment_manifest.mjs";

function verdict(result) {
  if (result.failure) throw new Error(result.failure);
  const v = JSON.parse(result.stdout);
  if (v.requested === "normal-completion") {
    if (result.code === 0 && v.status === "certified" && ["normal-completion", "all-outcomes"].includes(v.guarantee))
      return { guarantee: v.guarantee, scope: v.scope };
    if (result.code === 1 && v.status === "rejected" && typeof v.reason === "string")
      return { blocker: v.reason, ...(v.reason_code && { code: v.reason_code }) };
  }
  throw new Error("inconsistent lowersplit status or verdict: " + result.stdout);
}

export function certify({ stage, chunkFile, plan, want, run, lowersplit, nodeRun }) {
  const output = path.join(stage, "certified");
  const manifest = writeDeployment(path.join(stage, chunkFile), path.join(stage, "lean_spec.json"), output);
  const refusals = {};
  for (const mode of ["structural", "default"]) {
    const args = [
      manifest,
      output,
      "--require",
      "normal-completion",
      ...(mode === "structural" ? ["--structural"] : []),
    ];
    const emission = verdict(run(lowersplit, args));
    if (emission.blocker !== undefined) {
      refusals[mode] = emission;
      continue;
    }
    const disk = verdict(run(lowersplit, [...args, "--check-output"]));
    if (disk.blocker !== undefined) return { outcome: "error:disk", mode, emission, disk };
    const matches = (r) => !r.failure && r.code === 0 && r.stdout === want;
    const original = matches(nodeRun("original", chunkFile, stage));
    const emitted = matches(nodeRun("emitted", path.join(output, plan.entry), stage));
    return { outcome: original && emitted ? "certified" : "error:runtime", mode, emission, disk, original, emitted };
  }
  return { outcome: "rejected", refusals };
}
