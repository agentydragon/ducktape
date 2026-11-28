import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expectationLedger, observeExpectations } from "./expectations.mjs";

const partial = { stdout: "", source: { "main/entry.js": { contains: ["value"] } }, check_all_js: true, future: true };
for (const status of ["unchecked", "refused"]) {
  const items = expectationLedger(partial, status);
  for (const name of ["source:main/entry.js", "check_all_js", "unrepresented:future"])
    assert.equal(items.find((item) => item.assertion === name).status, status);
  assert.equal(items.find((item) => item.assertion === "exports").status, "inapplicable");
}

const root = process.env.TEST_TMPDIR;
const out = path.join(root, "report.jsonl");
const keep = path.join(root, "kept");
const runner = fileURLToPath(new URL("./run_cases.mjs", import.meta.url));
const result = spawnSync(
  process.execPath,
  [runner, "--certify", "--filter", "emission/catchall_exports/unclaimed", "--out", out, "--keep", keep],
  { encoding: "utf8" }
);
assert.equal(result.status, 0, result.stdout + result.stderr);
const row = JSON.parse(readFileSync(out, "utf8").trim());
assert.equal(row.id, "emission/catchall_exports/unclaimed");
assert.equal(row.outcome, "certified");
assert.equal(row.behavior, "pass");
assert(
  row.expectations.every(({ status }) => ["pass", "inapplicable"].includes(status)),
  JSON.stringify(row)
);
const exports = row.expectations.find(({ assertion }) => assertion === "exports:main/entry.js");
assert.deepEqual(exports.detail.original, ["foo", "x-y"]);
assert.deepEqual(exports.detail.emitted, ["foo", "x-y"]);
assert.equal(row.expectations.find(({ assertion }) => assertion === "probes:read.probe.mjs").status, "pass");

// Alter only the staged copy after its successful independent disk check.
const stage = path.join(keep, row.id);
const output = path.join(stage, "certified");
const entry = path.join(output, row.entry);
const bytes = readFileSync(entry, "utf8");
const wrong = bytes.replace(/\bfoo\b/, "f00");
assert.notEqual(wrong, bytes);
writeFileSync(entry, wrong);
const run = (cmd, args, opts) => {
  const r = spawnSync(cmd, args, { encoding: "utf8", ...opts });
  return { code: r.status, stdout: r.stdout ?? "", stderr: r.stderr ?? "", failure: r.error?.message ?? null };
};
const nodeRun = (_, file, cwd) => run(process.execPath, [file], { cwd });
const accept = {
  stdout: "",
  exports: { "main/entry.js": { include: ["foo", "x-y"] } },
  probes: { "read.probe.mjs": { stdout: "1 1\n" } },
};
const observed = observeExpectations({ accept, stage, chunkFile: "main.js", output, entry: row.entry, run, nodeRun });
assert.equal(observed.find(({ assertion }) => assertion === "exports:main/entry.js").status, "fail");
