import { strict as assert } from "node:assert";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = process.env.TEST_TMPDIR;
assert(root, "TEST_TMPDIR is required");
const output = path.join(root, "library-split-dumps");
const prepare = fileURLToPath(new URL("./prepare.mjs", import.meta.url));
const result = spawnSync(process.execPath, [prepare, output, "acorn", "semver"], {
  encoding: "utf8",
  timeout: 120_000,
});
assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);

const hashes = Object.fromEntries(
  ["acorn", "semver"].map((name) => [
    name,
    createHash("sha256")
      .update(readFileSync(path.join(output, `${name}.json`)))
      .digest("hex"),
  ])
);
assert.deepEqual(hashes, {
  acorn: "d52e61e0438447fd4fe22c8bc25337a66e461ea0f60f72e11664fb44449c91ed",
  semver: "7977a30a0bd533279013e1c069a6203ea64335f5247eb990d1eeab23d4b74f60",
});
console.log(JSON.stringify(hashes));
