import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";

const result = path.join(process.env.TEST_TMPDIR, "provider.jsonl");
const id = "emission/return_object_provider_import/spec";
execFileSync(path.resolve(process.env.CASE_RUNNER), ["--filter", id, "--out", result, "--regression"], {
  stdio: "inherit",
});
const row = JSON.parse(readFileSync(result, "utf8"));
assert.equal(row.id, id);
assert.equal(row.outcome, "agree-accept");
assert(!row.unchecked.includes("runtime_files"));
