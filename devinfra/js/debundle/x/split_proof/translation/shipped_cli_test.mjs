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

function refuse(args, env = process.env) {
  const result = spawnSync(process.env.LOWERSPLIT, args, { encoding: "utf8", env });
  if (result.error) throw result.error;
  const log = result.stdout + result.stderr;
  assert.equal(result.status, 1, log);
  const verdict = JSON.parse(result.stdout);
  assert.equal(verdict.status, "rejected", log);
  assert.equal(verdict.requested, "all-outcomes", log);
  assert.equal(verdict.guarantee, null, log);
  assert.equal(verdict.every_ending_matches, false, log);
  return verdict;
}

const out = path.join(work, "out");
const manifest = writeDeployment(source, spec, out);
const verdict = refuse([manifest, out]);
assert.equal(verdict.reason_code, "no_constructed_rule");
assert(!existsSync(out), "refusal created a destination");

mkdirSync(out);
const prior = path.join(out, "entry.mjs");
writeFileSync(prior, "export const answer = 0;\n");
assert.deepEqual(refuse([manifest, out]), verdict);
assert.deepEqual(readdirSync(out), ["entry.mjs"]);
assert.equal(readFileSync(prior, "utf8"), "export const answer = 0;\n");

const censusReport = path.join(work, "census.jsonl");
const censusEnv = {
  ...process.env,
  SPLIT_PROOF_STRATEGY_CENSUS: "1",
  SPLIT_PROOF_CENSUS_FORCE_LINKED: "1",
  SPLIT_PROOF_STRATEGY_CENSUS_OUT: censusReport,
};
const censusOut = path.join(work, "with-old-env");
assert.deepEqual(refuse([manifest, censusOut], censusEnv), verdict);
assert(!existsSync(censusOut), "old census variables created a destination");
assert(!existsSync(censusReport), "old census variables launched a strategy census");

for (const [name, argumentsFor] of [
  ["diagnostic", (out) => ["--diagnostic", manifest, out]],
  ["forced-diagnostic", (out) => ["--diagnostic", manifest, out, "--force"]],
  ["force", (out) => [manifest, out, "--force"]],
  ["check-output", (out) => [manifest, out, "--check-output"]],
  ["instance", (out) => ["--generate-instance", manifest, out]],
]) {
  const out = path.join(work, name);
  assert.match(refuse(argumentsFor(out)).reason, /usage: lowersplit/);
  assert(!existsSync(out), `${name} created its destination`);
  if (name === "instance") {
    writeFileSync(out, "keep this Lean file");
    assert.match(refuse(argumentsFor(out)).reason, /usage: lowersplit/);
    assert.equal(readFileSync(out, "utf8"), "keep this Lean file");
  } else {
    mkdirSync(out);
    const existing = path.join(out, "entry.mjs");
    writeFileSync(existing, "keep this JavaScript file");
    assert.match(refuse(argumentsFor(out)).reason, /usage: lowersplit/);
    assert.deepEqual(readdirSync(out), ["entry.mjs"]);
    assert.equal(readFileSync(existing, "utf8"), "keep this JavaScript file");
  }
}
