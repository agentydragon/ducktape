import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { split } from "../../translation/testing/split.mjs";

const resolve = createRequire(import.meta.url).resolve;
const source = resolve("lodash-es/isLength.js");
const spec = fileURLToPath(new URL("./lodash_is_length/split.json", import.meta.url));
const entry = "isLength.js";
const tmp = process.env.TEST_TMPDIR;

// Each generated tree contains exactly the same files. Change the ancestor
// package outside the tree that --check-output examines.
function fixture(type) {
  const parent = path.join(tmp, type);
  mkdirSync(parent);
  writeFileSync(path.join(parent, "package.json"), JSON.stringify({ type }));
  return { source, spec, out: path.join(parent, "split"), require: "all-outcomes" };
}

function nodeCall(file) {
  const run = spawnSync(
    process.execPath,
    ["--input-type=module", "-e", "const {default: f} = await import(process.argv[1]); console.log(f(3));", file],
    { encoding: "utf8" }
  );
  if (run.error) throw run.error;
  return run;
}

const original = nodeCall(source);
assert.equal(original.status, 0, original.stderr);
assert.equal(original.stdout, "true\n");
const esm = fixture("module");
const commonjs = fixture("commonjs");
const published = [esm, commonjs].map((request) => split(request));
const reread = [esm, commonjs].map((request) => split({ ...request, checkOutput: true }));

for (const result of [...published, ...reread]) {
  assert.equal(result.status, "certified", result.log);
  assert.equal(result.scope, "indexed-default-snapshot");
  assert.equal(result.guarantee, "all-outcomes");
  assert.equal(result.verdict.every_ending_matches, true);
  assert.deepEqual(result.files, [entry, "limits.mjs"]);
}
for (const file of published[0].files) {
  assert.deepEqual(readFileSync(path.join(esm.out, file)), readFileSync(path.join(commonjs.out, file)));
}
assert.deepEqual(published[0].verdict, published[1].verdict);
assert.deepEqual(reread[0].verdict, reread[1].verdict);

const good = nodeCall(path.join(esm.out, entry));
assert.equal(good.status, 0, good.stderr);
assert.equal(good.stdout, original.stdout);
const bad = nodeCall(path.join(commonjs.out, entry));
assert.notEqual(bad.status, 0);
assert.match(bad.stderr, /Cannot use import statement outside a module/);

console.log(
  JSON.stringify({
    module: { publish: published[0].verdict, reread: reread[0].verdict, node: good.stdout.trim() },
    commonjs: { publish: published[1].verdict, reread: reread[1].verdict, node: "SyntaxError" },
  })
);
