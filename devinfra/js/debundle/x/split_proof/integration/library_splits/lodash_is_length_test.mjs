import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { copyFileSync, cpSync, mkdirSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import isArrayLike from "lodash-es/isArrayLike.js";
import * as isLengthModule from "lodash-es/isLength.js";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";
import * as snapshot from "./lodash_is_length/default_expression.mjs";
import * as live from "./lodash_is_length/default_export_list.mjs";

const resolve = createRequire(import.meta.url).resolve;
const packageJson = JSON.parse(readFileSync(resolve("lodash-es/package.json"), "utf8"));
assert.equal(packageJson.name, "lodash-es");
assert.equal(packageJson.version, "4.18.1");
assert.equal(packageJson.type, "module");

function checkedSource(file, size, sha256) {
  const source = resolve("lodash-es/" + file);
  const bytes = readFileSync(source);
  assert.equal(bytes.length, size, file);
  assert.equal(createHash("sha256").update(bytes).digest("hex"), sha256, file);
  return { source, code: bytes.toString("utf8") };
}

const { source, code } = checkedSource(
  "isLength.js",
  800,
  "ef8193123dc3fbd12be9893aa39aea7df0a779074e604d853c56da1c09d24e11"
);
const consumer = checkedSource(
  "isArrayLike.js",
  828,
  "46956ba526a01c3371fe26a492bf2a247b20d995e6891f2ac99a17b1ab64b49f"
);

const parsed = parseModule(code, acorn.parse, eslintScope);
assert.deepEqual(parsed.scopes.globalScope.through, []);
const rows = topLevelStatements(parsed.ast).map(({ node }) => node);
assert.deepEqual(
  rows.map(({ type }) => type),
  ["VariableDeclaration", "FunctionDeclaration", "ExportDefaultDeclaration"]
);
assert.equal(rows[0].kind, "var");
assert.equal(rows[0].declarations[0].id.name, "MAX_SAFE_INTEGER");
assert.equal(rows[1].id.name, "isLength");
assert.equal(rows[2].declaration.type, "Identifier");
assert.equal(rows[2].declaration.name, "isLength");

assert.deepEqual(Object.keys(isLengthModule), ["default"]);
function observe(isLength, isArrayLike) {
  for (const [length, expected] of [
    [0, true],
    [3, true],
    [9007199254740991, true],
    [9007199254740992, false],
    [3.5, false],
    ["3", false],
    [-1, false],
  ]) {
    let reads = 0;
    const value = {
      get length() {
        reads += 1;
        return length;
      },
    };
    assert.equal(isLength(length), expected, String(length));
    assert.equal(isArrayLike(value), expected, String(length));
    assert.equal(reads, 1, String(length));
  }
  assert.equal(
    isArrayLike((value) => value),
    false
  );
}
observe(isLengthModule.default, isArrayLike);

// The shipped module never reassigns isLength. These separate modules make
// snapshot vs live alias observable through the same public name.
assert.equal(snapshot.default, 1);
assert.equal(live.default, 1);
snapshot.replace(2);
live.replace(2);
assert.equal(snapshot.default, 1);
assert.equal(live.default, 2);

const tmp = process.env.TEST_TMPDIR;
const readModule = (file) => import(pathToFileURL(file).href);
const specFor = (name) => fileURLToPath(new URL(`./lodash_is_length/${name}.json`, import.meta.url));
const request = {
  source,
  spec: specFor("split"),
  out: path.join(tmp, "split"),
  require: "all-outcomes",
};
const result = split(request);
assert.equal(result.status, "certified", result.log);
assert.equal(result.guarantee, "all-outcomes");
assert.equal(result.scope, "indexed-default-snapshot");
assert.deepEqual(result.files, ["isLength.js", "limits.mjs"]);
assert.deepEqual(split({ ...request, checkOutput: true }).verdict, result.verdict);

const misplaced = split({ ...request, spec: specFor("misplaced_export"), out: path.join(tmp, "misplaced") });
assert.equal(misplaced.status, "refused", misplaced.log);
assert.match(misplaced.reason, /executable default export must stay in the entry/);
assert.equal(split({ ...request, spec: specFor("misplaced_export"), allowExistingOut: true }).status, "refused");

// Run the unchanged original consumer with its original package config and
// isFunction dependency. Only isLength is supplied by the emitted split.
const consumerDir = path.join(tmp, "consumer");
mkdirSync(consumerDir);
copyFileSync(resolve("lodash-es/package.json"), path.join(consumerDir, "package.json"));
copyFileSync(consumer.source, path.join(consumerDir, "isArrayLike.js"));
symlinkSync(resolve("lodash-es/isFunction.js"), path.join(consumerDir, "isFunction.js"));
for (const file of result.files) copyFileSync(path.join(request.out, file), path.join(consumerDir, file));
const emitted = await readModule(path.join(consumerDir, "isLength.js"));
const originalCaller = await readModule(path.join(consumerDir, "isArrayLike.js"));
assert.deepEqual(Object.keys(emitted), ["default"]);
observe(emitted.default, originalCaller.default);
const emittedCode = readFileSync(path.join(request.out, "isLength.js"), "utf8");
const retainedRows = parseModule(emittedCode, acorn.parse, eslintScope).ast.body;
assert.deepEqual(
  retainedRows.map(({ type }) => type),
  ["ImportDeclaration", "FunctionDeclaration", "ExportDefaultDeclaration"]
);
assert.equal(rows[2].declaration.name, retainedRows[2].declaration.name);
assert.equal(emittedCode.slice(retainedRows[2].start, retainedRows[2].end), code.slice(rows[2].start, rows[2].end));
console.log(JSON.stringify({ workload: "whole lodash-es/isLength.js", verdict: result.verdict, files: result.files }));

const text =
  "var bound = 10;\nfunction within(n) { return n <= bound; }\n" +
  "export default within;\nwithin = function (n) { return false; };\n";
const changedSource = path.join(tmp, "reassign.mjs");
const changedSpec = path.join(tmp, "reassign.json");
writeFileSync(changedSource, text);
writeFileSync(
  changedSpec,
  JSON.stringify({
    entry: "reassign.mjs",
    modules: { "limits.mjs": ["#0"], "reassign.mjs": ["#1", "#2", "#3"] },
    order: ["limits.mjs"],
    sequential: true,
  })
);
const reassign = { ...request, source: changedSource, spec: changedSpec, out: path.join(tmp, "reassign") };
const snap = split(reassign);
assert.equal(snap.status, "certified", snap.log);
assert.deepEqual(split({ ...reassign, checkOutput: true }).verdict, snap.verdict);
assert.equal((await readModule(changedSource)).default(3), true);
assert.equal((await readModule(path.join(reassign.out, "reassign.mjs"))).default(3), true);

const wrong = path.join(tmp, "live-alias");
cpSync(reassign.out, wrong, { recursive: true });
const wrongFile = path.join(wrong, "reassign.mjs");
const wrongCode = readFileSync(wrongFile, "utf8");
const exportNode = parseModule(wrongCode, acorn.parse, eslintScope).ast.body.find(
  ({ type }) => type === "ExportDefaultDeclaration"
);
assert(exportNode);
writeFileSync(
  wrongFile,
  wrongCode.slice(0, exportNode.start) + "export { within as default };" + wrongCode.slice(exportNode.end)
);
assert.equal((await readModule(wrongFile)).default(3), false);
const refused = split({ ...reassign, out: wrong, checkOutput: true });
assert.equal(refused.status, "refused", refused.log);
assert.match(refused.reason, /reassign\.mjs/);

// Keeping the same export expression but evaluating it after the reassignment
// snapshots a different function. The statement must retain its ordinal.
const reordered = path.join(tmp, "late-default");
cpSync(reassign.out, reordered, { recursive: true });
const reorderedFile = path.join(reordered, "reassign.mjs");
writeFileSync(
  reorderedFile,
  wrongCode.slice(0, exportNode.start) +
    wrongCode.slice(exportNode.end) +
    "\n" +
    wrongCode.slice(exportNode.start, exportNode.end) +
    "\n"
);
assert.equal((await readModule(reorderedFile)).default(3), false);
assert.equal(split({ ...reassign, out: reordered, checkOutput: true }).status, "refused");
