import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { writeDeployment } from "../translation/testing/deployment_manifest.mjs";
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
  const run = spawnSync(
    process.env.LOWERSPLIT,
    [writeDeployment(source, spec, dir), dir, "--require", request, ...args],
    {
      encoding: "utf8",
    }
  );
  assert.equal(run.status, status === "certified" ? 0 : 1, run.stdout + run.stderr);
  const verdict = JSON.parse(run.stdout);
  assert.equal(verdict.requested, request);
  assert.equal(verdict.status, status);
  if (status === "certified") {
    assert([request, "all-outcomes"].includes(verdict.guarantee));
    if (verdict.guarantee === "all-outcomes") assert.equal(verdict.scope, "linked-slice-order-kept");
  } else assert.equal(verdict.guarantee, null);
}
place({ [provider]: ["#0"], [entry]: ["#1"] });
const out = path.join(process.env.TEST_TMPDIR, "good");
check(out, "certified");
assert.deepEqual(
  Object.entries(await import(pathToFileURL(path.join(out, entry)))),
  Object.entries(await import(pathToFileURL(source)))
);
// Run the corpus consumer in a new realm, including after changing the provider.
function probe() {
  const script = "const {default: read} = await import(process.argv[1]); read(await import(process.argv[2]));";
  const run = spawnSync(
    process.execPath,
    [
      "--input-type=module",
      "-e",
      script,
      pathToFileURL(process.env.PROBE).href,
      pathToFileURL(path.join(out, entry)).href,
    ],
    { encoding: "utf8" }
  );
  assert.equal(run.status, 0, run.stderr);
  return run.stdout;
}
assert.equal(probe(), "1\n");
check(out, "certified", "--check-output");
const file = path.join(out, provider);
const body = readFileSync(file, "utf8");
const changed = body.replace("1", "2");
assert.notEqual(body, changed);
writeFileSync(file, changed);
assert.equal(probe(), "2\n");
check(out, "rejected", "--check-output");
place({ [entry]: ["#0"], [provider]: ["#1"] });
const bad = path.join(process.env.TEST_TMPDIR, "bad");
check(bad, "rejected");
assert(!existsSync(bad));
