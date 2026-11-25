import assert from "node:assert/strict";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const [exampleAst, exampleSpec, proofBundle, proofSpec] = process.argv.slice(2);
assert(exampleAst && exampleSpec && proofBundle && proofSpec, "fixture runfiles are required");
const root = process.env.TEST_TMPDIR;
assert(root, "TEST_TMPDIR is required");
mkdirSync(root, { recursive: true });

for (const [name, source, spec] of [
  ["lowered-example", exampleAst, exampleSpec],
  ["proof-fixture", proofBundle, proofSpec],
]) {
  const out = path.join(root, `${name}-out`);
  const plan = JSON.parse(readFileSync(spec, "utf8"));
  const manifest = `${out}.deployment.json`;
  writeFileSync(
    manifest,
    JSON.stringify({
      entry: plan.entry,
      chunks: [{ path: plan.entry, source: path.resolve(source), spec: path.resolve(spec) }],
    })
  );
  const result = spawnSync(process.env.LOWERSPLIT, [manifest, out], { encoding: "utf8" });
  assert([0, 1].includes(result.status), result.error?.message ?? result.stdout + result.stderr);
  const rows = result.stdout.trim().split(/\r?\n/);
  assert.equal(rows.length, 1, result.stdout + result.stderr);
  const verdict = JSON.parse(rows[0]);
  assert(["certified", "rejected"].includes(verdict.status), result.stdout + result.stderr);
  assert.equal(result.status, verdict.status === "certified" ? 0 : 1, result.stdout + result.stderr);
}
