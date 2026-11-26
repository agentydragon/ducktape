import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { lstatSync, readdirSync, readFileSync, readlinkSync } from "node:fs";
import path from "node:path";

function snapshot(file) {
  const stat = lstatSync(file, { bigint: true, throwIfNoEntry: false });
  if (!stat) return null;
  const identity = { ino: stat.ino, mtimeNs: stat.mtimeNs };
  if (stat.isDirectory()) {
    const entries = readdirSync(file)
      .sort()
      .map((name) => [name, snapshot(path.join(file, name))]);
    return { ...identity, entries };
  }
  if (stat.isSymbolicLink()) return { ...identity, link: readlinkSync(file) };
  assert(stat.isFile(), `unexpected fixture output type at ${file}`);
  return { ...identity, bytes: readFileSync(file) };
}

function assertRefused(manifest, out, flags, assertReason) {
  assert(process.env.LOWERSPLIT, "LOWERSPLIT is required");
  const before = snapshot(out);
  const run = spawnSync(process.env.LOWERSPLIT, [manifest, out, ...flags], { encoding: "utf8" });
  if (run.error) throw run.error;
  const log = run.stdout + run.stderr;
  assert.equal(run.status, 1, log);
  let verdict;
  try {
    verdict = JSON.parse(run.stdout);
  } catch (error) {
    throw new Error(`lowersplit returned no JSON verdict: ${log}`, { cause: error });
  }
  assert.equal(verdict.status, "rejected", log);
  assert.equal(verdict.requested, "all-outcomes", log);
  assert.equal(verdict.guarantee, null, log);
  assert.equal(verdict.every_ending_matches, false, log);
  assertReason(verdict, log);
  assert.deepEqual(snapshot(out), before, `public lowersplit ${flags.join(" ")} changed ${out}`);
  return verdict;
}

/** Require the public CLI to refuse an unsupported split without touching its destination. */
export function assertPublicEmissionRefused(manifest, out) {
  return assertRefused(manifest, out, [], (verdict, log) => {
    assert.equal(verdict.reason_code, "no_constructed_rule", log);
  });
}

/** Require the current public CLI to reject the check-output option without touching its destination. */
export function assertPublicRecheckRefused(manifest, out) {
  return assertRefused(manifest, out, ["--check-output"], (verdict, log) => {
    assert.match(verdict.reason, /check-output options are not accepted/, log);
  });
}
