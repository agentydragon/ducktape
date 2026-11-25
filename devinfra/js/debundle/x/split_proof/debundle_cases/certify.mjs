// Certify the same owner assignment used for diagnostic comparison, from the JS bytes.
import { writeFileSync } from "node:fs";
import path from "node:path";
import { writeDeployment } from "../translation/testing/deployment_manifest.mjs";

// A certificate process parses and links a whole deployment. Keep a finite
// deadline but allow for remote CI contention during emission and disk checks.
const certificateTimeoutMs = 90_000;

function verdict(result, phase) {
  if (result.failure) throw new Error(`${phase}: ${result.failure} after ${result.elapsedMs} ms`);
  const value = JSON.parse(result.stdout);
  if (result.code === 0 && value.status === "certified" && value.guarantee === "normal-equivalence") {
    if (
      typeof value.strategy !== "string" ||
      !Array.isArray(value.premises) ||
      typeof value.every_ending_matches !== "boolean"
    )
      throw new Error("certified verdict is missing its strategy evidence: " + result.stdout);
    return {
      strategy: value.strategy,
      premises: value.premises,
      everyEndingMatches: value.every_ending_matches,
      ...(value.strategy_attempts && { strategy_attempts: value.strategy_attempts }),
    };
  }
  if (result.code === 1 && value.status === "rejected" && typeof value.reason === "string")
    return {
      blocker: value.reason,
      ...(value.reason_code && { code: value.reason_code }),
      ...(value.strategy_attempts && { strategy_attempts: value.strategy_attempts }),
    };
  throw new Error("inconsistent lowersplit status or verdict: " + result.stdout);
}

export function certify({ stage, chunkFile, plan, want, providers, run, lowersplit, nodeRun }) {
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

  const args = [manifest, output, ...(deployment ? ["--deployment-origin", "https://corpus.example"] : [])];
  const emission = verdict(run(lowersplit, args, { timeout: certificateTimeoutMs }), "emission");
  if (emission.blocker !== undefined) return { outcome: "rejected", emission };
  const disk = verdict(run(lowersplit, [...args, "--check-output"], { timeout: certificateTimeoutMs }), "disk check");
  if (disk.blocker !== undefined) return { outcome: "error:disk", emission, disk };
  const matches = (result) => !result.failure && result.code === 0 && result.stdout === want;
  const original = matches(nodeRun("original", chunkFile, stage));
  const emitted = matches(nodeRun("emitted", path.join(output, entry), stage));
  return {
    outcome: original && emitted ? "certified" : "error:runtime",
    strategy: emission.strategy,
    premises: emission.premises,
    everyEndingMatches: emission.everyEndingMatches,
    ...(deployment && { origin: "https://corpus.example" }),
    emission,
    disk,
    original,
    emitted,
  };
}
