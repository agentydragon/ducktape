import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { writeDeployment } from "./deployment_manifest.mjs";

function filesUnder(directory) {
  if (!existsSync(directory)) return [];
  return readdirSync(directory, { withFileTypes: true })
    .flatMap((entry) => {
      const file = path.join(directory, entry.name);
      return entry.isDirectory() ? filesUnder(file).map((child) => path.join(entry.name, child)) : [entry.name];
    })
    .filter((file) => statSync(path.join(directory, file)).isFile())
    .sort();
}

function snapshotUnder(directory) {
  return filesUnder(directory).map((file) => {
    const stat = statSync(path.join(directory, file));
    return [file, stat.size, stat.mtimeMs];
  });
}

/** Run lowersplit's certified command for one fixture and normalize its JSON verdict. */
export function split({
  source,
  ast,
  manifest,
  spec,
  out,
  hostContract,
  require: guarantee,
  structural = false,
  checkOutput = false,
  allowExistingOut = false,
  deploymentOrigin,
  deploymentPreset,
  deploymentImportMap,
  timeoutMs,
}) {
  const inputCount = Number(source !== undefined) + Number(ast !== undefined);
  assert.equal(inputCount + Number(manifest !== undefined), 1, "provide source, ast, or a deployment manifest");
  assert(manifest || spec, "spec is required when source or ast is provided");
  assert(out, "out is required");
  assert(process.env.LOWERSPLIT, "LOWERSPLIT is required");
  const existed = existsSync(out);
  assert(
    checkOutput ? existed : !existed || allowExistingOut,
    checkOutput ? "checkOutput requires an existing output directory" : "out must not exist"
  );
  const before = allowExistingOut && existed ? snapshotUnder(out) : null;

  const input = source ?? ast;
  const deployment = manifest ?? writeDeployment(input, spec, out);
  const args = [deployment, out];
  if (guarantee) args.push("--require", guarantee);
  if (hostContract) args.push("--host-contract", hostContract);
  if (structural) args.push("--structural");
  if (deploymentOrigin) args.push("--deployment-origin", deploymentOrigin);
  if (deploymentPreset) args.push("--deployment-preset", deploymentPreset);
  if (deploymentImportMap) args.push("--deployment-import-map", deploymentImportMap);
  if (checkOutput) args.push("--check-output");

  const result = spawnSync(process.env.LOWERSPLIT, args, {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
    ...(timeoutMs ? { timeout: timeoutMs } : {}),
  });
  if (result.error) throw result.error;
  const log = result.stdout + result.stderr;
  assert([0, 1].includes(result.status), log);
  const lines = result.stdout.trim().split(/\r?\n/);
  assert.equal(lines.length, 1, `expected one JSON verdict line: ${log}`);
  let verdict;
  try {
    verdict = JSON.parse(lines.at(-1));
  } catch (error) {
    throw new Error(`lowersplit returned no JSON verdict: ${log}`, { cause: error });
  }
  assert(["certified", "rejected"].includes(verdict.status), log);
  assert.equal(verdict.requested, guarantee ?? "normal-completion", log);
  assert.equal(result.status, verdict.status === "certified" ? 0 : 1, log);

  const files = filesUnder(out);
  if (verdict.status === "rejected" && !checkOutput) {
    if (before) assert.deepEqual(snapshotUnder(out), before, "refusal changed an existing output path");
    else assert.equal(files.length, 0, "refusal emitted files");
  }
  if (verdict.status === "certified") assert(files.length > 0, "certified split emitted no files");

  return {
    status: verdict.status === "certified" ? "certified" : "refused",
    scope: verdict.scope ?? null,
    guarantee: verdict.guarantee ?? null,
    reason: verdict.reason ?? null,
    reasonCode: verdict.reason_code ?? null,
    files: verdict.status === "certified" ? files : [],
    stdout: result.stdout,
    stderr: result.stderr,
    log,
    verdict,
  };
}

/** Generate the Lean example instance without asking lowersplit to emit module previews. */
export function generateInstance({ source, ast, manifest, spec, out, preview }) {
  const inputCount = Number(source !== undefined) + Number(ast !== undefined);
  assert.equal(inputCount + Number(manifest !== undefined), 1, "provide source, ast, or a deployment manifest");
  assert(manifest || spec, "spec is required when source or ast is provided");
  assert(out, "out is required");
  assert(preview, "preview path is required");
  assert(process.env.SPLIT_INSTANCE_GENERATOR, "SPLIT_INSTANCE_GENERATOR is required");
  assert(!existsSync(preview), "preview path must not exist before instance generation");
  const input = source ?? ast;
  const deployment = manifest ?? writeDeployment(input, spec, preview);
  const result = spawnSync(process.env.SPLIT_INSTANCE_GENERATOR, [deployment, out], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.error) throw result.error;
  const log = result.stdout + result.stderr;
  assert.equal(result.status, 0, log);
  assert(!existsSync(preview), "instance generation emitted module previews");
  return { out, stdout: result.stdout, stderr: result.stderr, log };
}
