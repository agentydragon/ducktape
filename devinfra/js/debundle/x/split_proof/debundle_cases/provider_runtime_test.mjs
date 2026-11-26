import assert from "node:assert/strict";
import { copyFileSync, mkdirSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";

const root = process.env.TEST_TMPDIR;
assert(root, "TEST_TMPDIR is required");
const fixture = path.join(root, "provider-runtime");
mkdirSync(fixture, { recursive: true });
copyFileSync(process.env.MAIN_JS, path.join(fixture, "main.js"));
copyFileSync(process.env.PROVIDER_MODULE, path.join(fixture, "provider_module.js"));
writeFileSync(path.join(fixture, "package.json"), '{"type":"module"}\n');

// Exercise the original cross-file import directly. The former assertion was
// about an emitted split that this CLI no longer constructs.
const run = spawnSync(process.execPath, [path.join(fixture, "main.js")], {
  cwd: fixture,
  encoding: "utf8",
});
assert.equal(run.status, 0, run.stderr);
assert.equal(run.stdout, "a:b\n");
