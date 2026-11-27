import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
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
checkedSource("isArrayLike.js", 828, "46956ba526a01c3371fe26a492bf2a247b20d995e6891f2ac99a17b1ab64b49f");

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
  assert.equal(isLengthModule.default(length), expected, String(length));
  assert.equal(isArrayLike(value), expected, String(length));
  assert.equal(reads, 1, String(length));
}
assert.equal(
  isArrayLike((value) => value),
  false
);

// The shipped module never reassigns isLength. These separate modules make
// snapshot vs live alias observable through the same public name.
assert.equal(snapshot.default, 1);
assert.equal(live.default, 1);
snapshot.replace(2);
live.replace(2);
assert.equal(snapshot.default, 1);
assert.equal(live.default, 2);

for (const placement of ["split", "misplaced_export"]) {
  const result = split({
    source,
    spec: fileURLToPath(new URL(`./lodash_is_length/${placement}.json`, import.meta.url)),
    out: path.join(process.env.TEST_TMPDIR, placement),
    require: "all-outcomes",
  });
  // split verifies exit code 1 and that refusal does not create the output path.
  assert.equal(result.status, "refused", result.log);
  assert.equal(result.reasonCode, "no_constructed_rule", result.log);
  assert.equal(result.guarantee, null, result.log);
}
