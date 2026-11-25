// Certify the same owner assignment used for diagnostic comparison, from the JS bytes.
import { writeFileSync } from "node:fs";
import path from "node:path";
import { writeDeployment } from "../translation/testing/deployment_manifest.mjs";

// A certificate process parses and links a whole deployment. Keep a finite
// deadline but allow for remote CI contention during emission and disk checks.
const certificateTimeoutMs = 90_000;

function verdict(result, phase) {
  if (result.failure) throw new Error(`${phase}: ${result.failure} after ${result.elapsedMs} ms`);
  const v = JSON.parse(result.stdout);
  if (v.requested === "normal-completion") {
    if (result.code === 0 && v.status === "certified" && ["normal-completion", "all-outcomes"].includes(v.guarantee))
      return { guarantee: v.guarantee, scope: v.scope };
    if (result.code === 1 && v.status === "rejected" && typeof v.reason === "string")
      return { blocker: v.reason, ...(v.reason_code && { code: v.reason_code }) };
  }
  throw new Error("inconsistent lowersplit status or verdict: " + result.stdout);
}

export function certify({ stage, chunkFile, plan, want, providers, run, lowersplit, nodeRun, hostContract }) {
  const output = path.join(stage, "certified");
  const spec = path.join(stage, "certificate-spec.json");
  let manifest,
    entry = plan.entry;
  // Retain the directory of a nested entry and its fixed source modules.
  // Synthetic filenames live beside that entry in the same deployment.
  const deployment = providers.length > 0 && path.dirname(chunkFile) !== ".";
  if (deployment) {
    entry = chunkFile;
    const route = (name) => (name === plan.entry ? entry : path.posix.join(path.dirname(entry), name));
    const modules = Object.fromEntries(Object.entries(plan.modules).map(([name, units]) => [route(name), units]));
    writeFileSync(spec, JSON.stringify({ entry, modules, order: plan.order.map(route), privateEntryBody: true }));
    const fixed = [...new Set(providers)].map((file) => {
      if (path.isAbsolute(file) || file.split("/").some((s) => !s || s === "." || s === ".."))
        throw new Error("invalid fixed provider route: " + file);
      return { path: file, source: path.join(stage, file) };
    });
    manifest = path.join(stage, "deployment.json");
    writeFileSync(
      manifest,
      JSON.stringify({ entry, chunks: [{ path: entry, source: path.join(stage, chunkFile), spec }, ...fixed] })
    );
  } else {
    writeFileSync(spec, JSON.stringify({ ...plan, privateEntryBody: true }));
    manifest = writeDeployment(path.join(stage, chunkFile), spec, output);
  }
  const refusals = {};
  for (const mode of deployment ? ["structural"] : ["structural", "default"]) {
    const args = [
      manifest,
      output,
      "--require",
      "normal-completion",
      ...(hostContract ? ["--host-contract", hostContract] : []),
      ...(mode === "structural" ? ["--structural"] : []),
      ...(deployment ? ["--deployment-origin", "https://corpus.example"] : []),
    ];
    const emission = verdict(run(lowersplit, args, { timeout: certificateTimeoutMs }), `${mode} emission`);
    if (emission.blocker !== undefined) {
      refusals[mode] = emission;
      continue;
    }
    const disk = verdict(
      run(lowersplit, [...args, "--check-output"], { timeout: certificateTimeoutMs }),
      `${mode} disk check`
    );
    if (disk.blocker !== undefined) return { outcome: "error:disk", mode, emission, disk };
    const matches = (r) => !r.failure && r.code === 0 && r.stdout === want;
    const original = matches(nodeRun("original", chunkFile, stage));
    const emitted = matches(nodeRun("emitted", path.join(output, entry), stage));
    return {
      outcome: original && emitted ? "certified" : "error:runtime",
      mode,
      ...(deployment && { origin: "https://corpus.example" }),
      emission,
      disk,
      original,
      emitted,
    };
  }
  return { outcome: "rejected", refusals };
}
