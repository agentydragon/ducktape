import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { writeDeployment } from "./testing/deployment_manifest.mjs";

const work = mkdtempSync(path.join(process.env.TEST_TMPDIR, "shipped-cli-"));
const source = path.join(work, "bundle.mjs");
const spec = path.join(work, "spec.json");
writeFileSync(source, "export const answer = 42;\n");
writeFileSync(spec, JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#0"] } }));

function run(args, expected, env = process.env) {
  const result = spawnSync(process.env.LOWERSPLIT, args, { encoding: "utf8", env });
  if (result.error) throw result.error;
  const log = result.stdout + result.stderr;
  assert.equal(result.status, expected === "certified" ? 0 : 1, log);
  const verdict = JSON.parse(result.stdout);
  assert.equal(verdict.status, expected, log);
  return verdict;
}

const certified = path.join(work, "certified");
const manifest = writeDeployment(source, spec, certified);
const verdict = run([manifest, certified], "certified");
assert.deepEqual(run([manifest, certified, "--check-output"], "certified"), verdict);

const censusReport = path.join(work, "census.jsonl");
const censusEnv = {
  ...process.env,
  SPLIT_PROOF_STRATEGY_CENSUS: "1",
  SPLIT_PROOF_CENSUS_FORCE_LINKED: "1",
  SPLIT_PROOF_STRATEGY_CENSUS_OUT: censusReport,
};
assert.deepEqual(run([manifest, path.join(work, "with-old-env")], "certified", censusEnv), verdict);
assert(!existsSync(censusReport), "old census variables launched a strategy census");

for (const [name, argumentsFor] of [
  ["diagnostic", (out) => ["--diagnostic", manifest, out]],
  ["forced-diagnostic", (out) => ["--diagnostic", manifest, out, "--force"]],
  ["force", (out) => [manifest, out, "--force"]],
  ["instance", (out) => ["--generate-instance", manifest, out]],
]) {
  const out = path.join(work, name);
  run(argumentsFor(out), "rejected");
  assert(!existsSync(out), `${name} created its destination`);
  if (name === "instance") {
    writeFileSync(out, "keep this Lean file");
    run(argumentsFor(out), "rejected");
    assert.equal(readFileSync(out, "utf8"), "keep this Lean file");
  } else {
    mkdirSync(out);
    const existing = path.join(out, "entry.mjs");
    writeFileSync(existing, "keep this JavaScript file");
    run(argumentsFor(out), "rejected");
    assert.deepEqual(readdirSync(out), ["entry.mjs"]);
    assert.equal(readFileSync(existing, "utf8"), "keep this JavaScript file");
  }
}
