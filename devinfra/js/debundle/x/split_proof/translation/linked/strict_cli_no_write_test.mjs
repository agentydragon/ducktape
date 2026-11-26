import assert from "node:assert/strict";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { writeDeployment } from "../testing/deployment_manifest.mjs";

const root = process.env.TEST_TMPDIR;
assert(root, "TEST_TMPDIR is required");
const lowersplit = process.env.LOWERSPLIT;
assert(lowersplit, "LOWERSPLIT is required");

const source = path.join(root, "unsupported.mjs");
const spec = path.join(root, "unsupported-spec.json");
writeFileSync(source, "const seed = Math.random(); export const result = seed;\n");
writeFileSync(
  spec,
  JSON.stringify({
    entry: "entry.mjs",
    modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1"] },
    order: ["worker.mjs"],
    sequential: true,
  })
);

function assertRefused(out, args, requested) {
  const manifest = writeDeployment(source, spec, out);
  const result = spawnSync(lowersplit, [manifest, out, ...args], { encoding: "utf8" });
  assert.ifError(result.error);
  assert.equal(result.status, 1, result.stdout + result.stderr);
  const lines = result.stdout.trim().split(/\r?\n/);
  assert.equal(lines.length, 1, result.stdout + result.stderr);
  const verdict = JSON.parse(lines[0]);
  assert.equal(verdict.status, "rejected");
  assert.equal(verdict.requested, requested);
  assert.equal(verdict.guarantee, null);
  assert.equal(verdict.reason_code, "no_constructed_rule");
  assert.match(verdict.reason, /no JavaScript was written/);
}

// An unsupported initializer is refused without creating output.
const freshOut = path.join(root, "fresh-output");
assertRefused(freshOut, [], "all-outcomes");
assert(!existsSync(freshOut), "refusal created a fresh output path");

// Requesting the weaker guarantee with --check-output still refuses without changing existing bytes.
const existingOut = path.join(root, "existing-output");
mkdirSync(existingOut);
const sentinel = path.join(existingOut, "sentinel.bin");
const bytes = Buffer.from([0x00, 0x13, 0x7f, 0xff]);
writeFileSync(sentinel, bytes);
const before = readdirSync(existingOut)
  .sort()
  .map((name) => [name, readFileSync(path.join(existingOut, name))]);
assertRefused(existingOut, ["--check-output", "--require", "normal-completion"], "normal-completion");
const after = readdirSync(existingOut)
  .sort()
  .map((name) => [name, readFileSync(path.join(existingOut, name))]);
assert.deepEqual(after, before, "refusal changed bytes in the existing output path");
