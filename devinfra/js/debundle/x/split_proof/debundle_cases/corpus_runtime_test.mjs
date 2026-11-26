import assert from "node:assert/strict";
import { copyFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { pathToFileURL } from "node:url";

const root = process.env.TEST_TMPDIR;
assert(root, "TEST_TMPDIR is required");
const app = path.join(root, "app.mjs");
copyFileSync(process.env.CHUNK, app);

// Keep the corpus's existing consumer probe and run it against the original
// source module. This preserves the independent Node runtime observation
// without asking the retired certificate/output path to construct modules.
const run = spawnSync(
  process.execPath,
  [
    "--input-type=module",
    "-e",
    "const {default: read} = await import(process.argv[1]); read(await import(process.argv[2]));",
    pathToFileURL(path.resolve(process.env.PROBE)).href,
    pathToFileURL(app).href,
  ],
  { encoding: "utf8" }
);
assert.equal(run.status, 0, run.stderr);
assert.equal(run.stdout, "1\n");

const original = await import(pathToFileURL(app).href);
assert.deepEqual(Object.keys(original), ["a"]);
assert.equal(original.a, 1);
