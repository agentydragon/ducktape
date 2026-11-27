import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import sortedUniq from "lodash-es/sortedUniq.js";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

const resolve = createRequire(import.meta.url).resolve;
const manifest = resolve("lodash-es/package.json");
const { name, version, type } = JSON.parse(readFileSync(manifest, "utf8"));
assert.deepEqual({ name, version, type }, { name: "lodash-es", version: "4.18.1", type: "module" });

const sources = new Map();
for (const [file, size, sha256] of [
  ["_baseSortedUniq.js", 756, "0cb97ac908e675c379134659f8814fd244017ef0c401913c3f6bb6282feb69ef"],
  ["eq.js", 797, "3a51729bde3af1d9bacf9b67e966d8c154e22b02f5e1cecc13f53f7f00889c13"],
  ["sortedUniq.js", 511, "0b23aa21cf887a1db228d20ecc388f5cc4ad6a0f40a3e8935d4aa44410a69c95"],
]) {
  const source = resolve("lodash-es/" + file);
  const bytes = readFileSync(source);
  assert.equal(bytes.length, size, file);
  assert.equal(createHash("sha256").update(bytes).digest("hex"), sha256, file);
  const parsed = parseModule(bytes.toString("utf8"), acorn.parse, eslintScope);
  assert.deepEqual(parsed.scopes.globalScope.through, [], file);
  sources.set(file, { source, parsed });
}

const entry = "_baseSortedUniq.js";
const target = sources.get(entry);
const rows = topLevelStatements(target.parsed.ast).map(({ node }) => node);
assert.deepEqual(
  rows.map(({ type }) => type),
  ["ImportDeclaration", "FunctionDeclaration", "ExportDefaultDeclaration"]
);
assert.equal(rows[0].source.value, "./eq.js");
assert.equal(rows[0].specifiers[0].type, "ImportDefaultSpecifier");
assert.equal(rows[1].id.name, "baseSortedUniq");
assert.equal(rows[2].declaration.name, "baseSortedUniq");

const a = { value: 1 };
const b = { value: 1 };
const input = [-0, 0, NaN, NaN, a, a, b, b];
const uniq = sortedUniq(input);
assert.deepEqual(uniq, [0, NaN, a, b]);
assert.equal(Object.is(uniq[0], +0), true);
assert.equal(uniq[2], a);
assert.equal(uniq[3], b);
assert.notEqual(uniq[2], uniq[3]);
assert.notEqual(uniq, input);
assert.deepEqual(input, [-0, 0, NaN, NaN, a, a, b, b]);
assert.notEqual(sortedUniq([]), sortedUniq([]));

const observed = JSON.parse(
  execFileSync(process.env.LINKED_INPUT_CHECKER, {
    encoding: "utf8",
    input:
      JSON.stringify(
        [...sources].slice(0, 2).map(([entry, { parsed }]) => ({
          kind: "sourceClassification",
          input: { entry, source: dumpParsed(parsed, { includeOpaqueCaptures: true }) },
        }))
      ) + "\n",
  })
);
const [targetShape, providerShape] = observed.map(({ output }) => output);
assert.deepEqual(targetShape.imports, [["eq.js", [["default", "eq"]]]]);
assert.deepEqual(targetShape.units, [0, 1, 2]);
assert.deepEqual(targetShape.exports, [["default", "default"]]);
assert.deepEqual(targetShape.body, [
  { id: 1, bindings: ["baseSortedUniq"], free: ["baseSortedUniq", "eq"], functionDeclaration: true },
  { id: 2, bindings: ["default"], free: ["default", "baseSortedUniq"], functionDeclaration: false },
]);
assert.deepEqual(providerShape.imports, []);
assert.deepEqual(providerShape.units, [0, 1]);
assert.deepEqual(providerShape.exports, [["default", "default"]]);

const spec = fileURLToPath(new URL("./lodash_sorted_uniq/split.json", import.meta.url));
const request = {
  source: target.source,
  spec,
  out: path.join(process.env.TEST_TMPDIR, "split"),
  require: "all-outcomes",
};
const result = split(request);
assert.equal(result.status, "refused", result.log);
assert.equal(result.reasonCode, "no_constructed_rule", result.log);
assert.equal(result.reason, "static provider: expected a raw .mjs source and a root-level entry route");

// No certified tree exists. Check an existing directory containing only
// original package bytes; split verifies refusal leaves it unchanged.
const prior = path.join(process.env.TEST_TMPDIR, "prior");
mkdirSync(prior);
copyFileSync(manifest, path.join(prior, "package.json"));
for (const [file, { source }] of sources) copyFileSync(source, path.join(prior, file));
const recheck = split({ ...request, out: prior, checkOutput: true });
assert.deepEqual(recheck.verdict, result.verdict);
console.log(
  JSON.stringify({
    workload: "lodash-es/_baseSortedUniq.js",
    decoded: observed,
    split: result.verdict,
    recheck: recheck.verdict,
  })
);
