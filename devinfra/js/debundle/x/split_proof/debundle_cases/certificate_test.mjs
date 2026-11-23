import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const spec = path.join(process.env.TEST_TMPDIR, "spec.json");
const source = path.join(process.env.TEST_TMPDIR, "source.mjs");
writeFileSync(source, readFileSync(process.env.CHUNK));
const entry = "entry.mjs";
const provider = "provider.mjs";
function place(modules) {
  writeFileSync(spec, JSON.stringify({ entry, modules, order: [provider], sequential: true }));
}
function check(dir, status, ...args) {
  const request = "normal-completion";
  const run = spawnSync(process.env.LOWERSPLIT, [source, spec, dir, "--require", request, ...args], {
    encoding: "utf8",
  });
  assert.equal(run.status, status === "certified" ? 0 : 1, run.stdout + run.stderr);
  const verdict = JSON.parse(run.stdout);
  assert.equal(verdict.requested, request);
  assert.equal(verdict.status, status);
  assert.equal(verdict.guarantee, status === "certified" ? request : null);
}
place({ [provider]: ["#0"], [entry]: ["#1"] });
const out = path.join(process.env.TEST_TMPDIR, "good");
check(out, "certified");
assert.deepEqual(
  Object.entries(await import(pathToFileURL(path.join(out, entry)))),
  Object.entries(await import(pathToFileURL(source)))
);
check(out, "certified", "--check-output");
const file = path.join(out, provider);
const body = readFileSync(file, "utf8");
const changed = body.replace("1", "2");
assert.notEqual(body, changed);
writeFileSync(file, changed);
check(out, "rejected", "--check-output");
place({ [entry]: ["#0"], [provider]: ["#1"] });
const bad = path.join(process.env.TEST_TMPDIR, "bad");
check(bad, "rejected");
assert(!existsSync(bad));
