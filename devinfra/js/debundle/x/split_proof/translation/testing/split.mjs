import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { lstatSync, readFileSync, readdirSync, readlinkSync } from "node:fs";
import path from "node:path";
import { writeDeployment } from "./deployment_manifest.mjs";

function outputStat(file) {
  try {
    return lstatSync(file, { throwIfNoEntry: false });
  } catch (error) {
    if (error.code === "ENOTDIR") return undefined;
    throw error;
  }
}

function inspectOutput(directory, stat, capture) {
  const files = [];
  const snapshot = [];
  function visit(file, relative, info) {
    if (info.isFile() && relative) files.push(relative);
    if (capture) {
      let bytes;
      if (info.isFile()) bytes = readFileSync(file);
      if (info.isSymbolicLink()) bytes = readlinkSync(file, { encoding: "buffer" });
      snapshot.push([relative, info.mode, info.mtimeMs, info.ctimeMs, bytes]);
    }
    if (info.isDirectory()) {
      for (const name of readdirSync(file).sort()) {
        const child = path.join(file, name);
        visit(child, path.join(relative, name), lstatSync(child));
      }
    }
  }
  if (stat) visit(directory, "", stat);
  return { files: files.sort(), snapshot };
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
  const originalOutput = outputStat(out);
  const existed = originalOutput !== undefined;
  assert(
    checkOutput ? existed : !existed || allowExistingOut,
    checkOutput ? "checkOutput requires an existing output directory" : "out must not exist"
  );
  const before =
    existed && (allowExistingOut || checkOutput) ? inspectOutput(out, originalOutput, true).snapshot : null;

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

  const after = outputStat(out);
  const compare = before !== null && (checkOutput || verdict.status === "rejected");
  const { files, snapshot } = inspectOutput(out, after, compare);
  if (compare) assert.deepEqual(snapshot, before, checkOutput ? "recheck changed output" : "refusal changed output");
  if (verdict.status === "rejected" && !existed) assert.equal(after, undefined, "refusal created the output path");
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
