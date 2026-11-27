import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

// Same pinned source, projection, and placement as #20604's consumer replay.
const pinnedAcorn = process.argv[2];
const { code, ast } = parseModule(readFileSync(pinnedAcorn, "utf8"), acorn.parse, eslintScope);
const units = topLevelStatements(ast);
const digest = (text) => createHash("sha256").update(text).digest("hex");
assert.equal(digest(code), "e2478dfecff35578381298798b067b66c0424057a99c4c6ddddc6f6b2fb45eee");
assert.equal(units.length, 447);
const decl = units[22].node;
assert.equal(decl.type, "FunctionDeclaration");
assert.equal(decl.id.name, "isNewLine");
assert.equal(decl.params.length, 1);
const fn = code.slice(decl.start, decl.end);
const row = units[446].node;
assert.equal(row.type, "ExportNamedDeclaration");
assert.equal(row.declaration, null);
assert.equal(row.source, null);
const [exported] = row.specifiers.filter((s) => s.exported.name === decl.id.name);
assert.equal(exported.local.name, decl.id.name);
const inputBytes = fn + "\nexport { " + code.slice(exported.start, exported.end) + " };\n";
assert.equal(digest(inputBytes), "add995a3f3dced274dbcaeb833465c1bc25e3b2d309042da5fd2d54ee0ffc494");
const helper = "m0.mjs";
const entry = "m1.mjs";
const dir = process.env.TEST_TMPDIR;
const put = (name, bytes) => {
  const filename = path.join(dir, name);
  writeFileSync(filename, bytes);
  return filename;
};
const source = put("line-break-predicate.mjs", inputBytes);
const out = path.join(dir, "ordinary");
const placement = { entry, modules: { [helper]: ["#0"], [entry]: ["#1"] }, order: [helper], sequential: true };
const writePlacement = (name, spec) => put(name + "-spec.json", JSON.stringify(spec));
const request = {
  source,
  spec: writePlacement("line-break-predicate", placement),
  out,
  require: "all-outcomes",
};

const arguments_ = [0, 10, 13, 32, 0x2028, 0x2029, "10", null, {}];
const expected = [false, true, true, false, true, true, false, false, false];
const callFresh = (file) =>
  JSON.parse(
    execFileSync(
      process.execPath,
      [
        "--input-type=module",
        "--eval",
        "const { isNewLine } = await import(process.argv[1]); console.log(JSON.stringify(JSON.parse(process.argv[2]).map((arg) => isNewLine(arg))));",
        pathToFileURL(file).href,
        JSON.stringify(arguments_),
      ],
      { encoding: "utf8" }
    )
  );

const result = split(request);
assert.equal(result.status, "certified", result.log);
assert.equal(result.guarantee, "all-outcomes");
assert.equal(result.scope, "indexed-exported-function");
assert.equal(result.verdict.strategy, "compiled-constructor");
assert.deepEqual(result.files, [helper, entry]);
assert.deepEqual(split({ ...request, checkOutput: true }).verdict, result.verdict);

const helperPath = path.join(out, helper);
const entryPath = path.join(out, entry);
const helperBytes = readFileSync(helperPath, "utf8");
const entryBytes = readFileSync(entryPath, "utf8");
assert(helperBytes.includes(fn), helperBytes);
const helperAst = acorn.parse(helperBytes, { ecmaVersion: "latest", sourceType: "module" });
const entryAst = acorn.parse(entryBytes, { ecmaVersion: "latest", sourceType: "module" });
assert.deepEqual(
  helperAst.body.map((s) => s.type),
  ["FunctionDeclaration", "ExportNamedDeclaration"]
);
assert.deepEqual(
  entryAst.body.map((s) => s.type),
  ["ImportDeclaration", "ExportNamedDeclaration"]
);
assert.equal(entryAst.body[0].source.value, "./" + helper);
assert.equal(entryAst.body[1].specifiers[0].local.name, decl.id.name);
assert.deepEqual(callFresh(pinnedAcorn), expected);
assert.deepEqual(callFresh(source), expected);
assert.deepEqual(callFresh(entryPath), expected);

function refused(options, reason) {
  const invalid = split(options);
  assert.equal(invalid.status, "refused", invalid.log);
  assert.match(invalid.reason, reason);
}

for (const [name, modules, reason] of [
  ["no-metadata", { [helper]: ["#0"], [entry]: [] }, /metadata row must be selected exactly once/],
  ["duplicate-metadata", { [helper]: ["#0"], [entry]: ["#1", "#1"] }, /original unit is selected more than once/],
  ["metadata-helper", { [helper]: ["#0", "#1"], [entry]: [] }, /row must be authored in the entry/],
  ["function-entry", { [helper]: [], [entry]: ["#0", "#1"] }, /captures must be authored in the helper/],
]) {
  refused({ ...request, spec: writePlacement(name, { ...placement, modules }), out: path.join(dir, name) }, reason);
}

// Probe equal-result disk edits with fresh subprocesses, so the ESM cache
// cannot disguise either a changed callable or a changed public cell.
function tamper(file, variant, reason) {
  const before = readFileSync(file, "utf8");
  assert.notEqual(variant, before);
  writeFileSync(file, variant);
  assert.deepEqual(callFresh(entryPath), expected);
  refused({ ...request, checkOutput: true }, reason);
  writeFileSync(file, before);
}
const param = decl.params[0].name;
const returnValue = decl.body.body[0].argument;
const valueText = code.slice(returnValue.start, returnValue.end);
tamper(helperPath, helperBytes.replace(valueText, "(" + valueText + ") || " + param + " === 10"), /m0\.mjs/);
const exportNode = entryAst.body[1];
const exportText = entryBytes.slice(exportNode.start, exportNode.end);
tamper(
  entryPath,
  entryBytes.replace(exportText, "const alternateCell = isNewLine;\nexport { alternateCell as isNewLine };"),
  /m1\.mjs/
);
const alternateFn = fn.slice(0, decl.id.start - decl.start) + "alternateCallable" + fn.slice(decl.id.end - decl.start);
tamper(
  entryPath,
  entryBytes.replace(exportText, alternateFn + "\nexport { alternateCallable as isNewLine };"),
  /m1\.mjs/
);
assert.deepEqual(split({ ...request, checkOutput: true }).verdict, result.verdict);

const coercive = put("coercive.mjs", "function predicate(value) { return value == 10; }\nexport { predicate };\n");
refused(
  { ...request, source: coercive, out: path.join(dir, "coercive") },
  /expected strict comparisons and logical expressions/
);
