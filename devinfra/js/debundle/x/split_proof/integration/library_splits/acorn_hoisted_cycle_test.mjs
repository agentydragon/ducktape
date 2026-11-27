import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";

const entry = "entry.mjs";
const scanner = "scanner.mjs";
const parse = (text) => parseModule(text, acorn.parse, eslintScope);
const digest = (text) => createHash("sha256").update(text).digest("hex");
const full = parse(readFileSync(process.argv[2], "utf8"));
assert.equal(digest(full.code), "e2478dfecff35578381298798b067b66c0424057a99c4c6ddddc6f6b2fb45eee");

// The exact B projection in #20625. The two pinned hashes make a dependency
// on a different upstream file or on a rearranged projection fail loudly.
const sourceIds = [22, 23, 35, 36, 38, 446];
const units = topLevelStatements(full.ast);
const [newline, scan, position, offset, info] = sourceIds.slice(0, -1).map((id) => {
  const { start, end, node } = units[id];
  assert.deepEqual([start, end], [node.start, node.end]);
  return full.code.slice(start, end);
});
const exportRow = units[sourceIds.at(-1)].node;
assert.equal(exportRow.type, "ExportNamedDeclaration");
const publicNames = ["getLineInfo", "isNewLine"];
const publication = exportRow.specifiers
  .filter((sp) => publicNames.includes(sp.exported.name))
  .map((sp) => full.code.slice(sp.start, sp.end))
  .join(", ");
const code = [newline, scan, position, offset, info, "export { " + publication + " };", ""].join("\n");
const projectionSha256 = digest(code);
assert.equal(projectionSha256, "c3e458d7a67d6a8b781b7d3ff8052bc135db59b36eeac620c470f9a01b2c83ab");
const projection = parse(code);
assert.deepEqual(projection.scopes.globalScope.through, []);
const spec = {
  entry,
  modules: { [scanner]: ["#1"], [entry]: ["#0", "#2", "#3", "#4", "#5"] },
  order: [scanner],
  sequential: true,
};

// Create physical layouts from the unchanged source slices. Lean re-reads
// their ASTs separately and checks the positive layout against the planner.
const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const fromScanner = 'import { nextLineBreak } from "./scanner.mjs";\n';
const fromEntry = 'import { isNewLine } from "./entry.mjs";\n';
const entryBody = [newline, position, offset, info].join("\n") + "\n";
const scannerBody = scan + "\nexport { nextLineBreak };\n";
const normalEntry = fromScanner + entryBody + "export { " + publication + " };\n";
function writeLayout(name, entryText, scannerText) {
  const dir = path.join(artifacts, name);
  mkdirSync(dir);
  writeFileSync(path.join(dir, entry), entryText);
  writeFileSync(path.join(dir, scanner), scannerText);
  return dir;
}
const normal = writeLayout("cycle", normalEntry, fromEntry + scannerBody);
const wrong = writeLayout(
  "wrong-cell",
  fromScanner + entryBody + "export { getLineInfo, getLineInfo as isNewLine };\n",
  fromEntry + scannerBody
);
const traced = writeLayout(
  "trace",
  fromScanner +
    'console.log("entry before", typeof Position, typeof nextLineBreak);\n' +
    normalEntry.slice(fromScanner.length) +
    '\nconsole.log("entry after", typeof Position, typeof Position.prototype.offset);\n',
  fromEntry + scannerBody + '\nconsole.log("scanner", typeof isNewLine, isNewLine(13));\n'
);
const call = 'getLineInfo("a\\nb", 2);\n';
const eager = writeLayout(
  "eager",
  normalEntry,
  'import { getLineInfo } from "./entry.mjs";\n' + fromEntry + scannerBody + call
);
const flat = path.join(artifacts, "flat-eager.mjs");
writeFileSync(flat, code + call);
writeFileSync(path.join(artifacts, "line-info.mjs"), code);
writeFileSync(path.join(artifacts, "B-spec.json"), JSON.stringify(spec, null, 2));

function run(file) {
  const result = spawnSync(process.execPath, [file], { encoding: "utf8" });
  if (result.error) throw result.error;
  return result;
}
for (const file of [path.join(normal, entry), flat]) {
  const result = run(file);
  assert.equal(result.status, 0, result.stderr);
}
const trace = run(path.join(traced, entry));
assert.equal(trace.status, 0, trace.stderr);
assert.equal(trace.stdout, "scanner function true\nentry before undefined function\nentry after function function\n");
const early = run(path.join(eager, entry));
assert.equal(early.status, 1, early.stderr);
assert.match(early.stderr, /TypeError: Position is not a constructor/);
const node = {
  sourceIds,
  projectionSha256,
  trace: trace.stdout.trimEnd().split("\n"),
  flatEagerStatus: 0,
  splitEagerStatus: early.status,
  splitEagerError: "TypeError: Position is not a constructor",
};
writeFileSync(path.join(artifacts, "node.json"), JSON.stringify(node, null, 2));
console.log(JSON.stringify({ node }));

const dump = (parsed) => dumpParsed(parsed, { includeOpaqueCaptures: true });
const dumpFiles = (dir) =>
  Object.fromEntries([entry, scanner].map((file) => [file, dump(parse(readFileSync(path.join(dir, file), "utf8")))]));
const linked = JSON.parse(
  execFileSync(process.env.CYCLE_CHECKER, {
    input:
      JSON.stringify({
        source: dump(projection),
        sourceText: code,
        spec,
        files: dumpFiles(normal),
        wrongFiles: dumpFiles(wrong),
        eagerSource: dump(parse(code + call)),
        eagerText: code + call,
        eagerFiles: dumpFiles(eager),
      }) + "\n",
    encoding: "utf8",
  })
);
assert.deepEqual(linked.physicalOrder, [scanner, entry]);
assert.deepEqual(linked.sourceRows, [0, 1, 2, 3, 4]);
assert.deepEqual(linked.authoredRows, [1, 0, 2, 3, 4, 5]);
assert.deepEqual(linked.executableRows, [1, 0, 2, 3, 4]);
assert.deepEqual(linked.sourceCells, {
  isNewLine: [1, 0],
  nextLineBreak: [2, 0],
  Position: [3, 0],
  getLineInfo: [5, 0],
});
assert.deepEqual(linked.candidateCells, linked.sourceCells);
assert.deepEqual(linked.wrongCapture, [5, 0]);
assert.equal(linked.wrongReason, "candidate is not the adjacent transposition with identical statement captures");
assert.deepEqual(linked.eager, {
  fuel: 128,
  flat: "ok",
  split: "TypeError: not a constructor",
  refusal: linked.wrongReason,
});
assert.equal(linked.guardReason, "authored plan does not enumerate every original source ordinal in order");
writeFileSync(path.join(artifacts, "linked.json"), JSON.stringify(linked, null, 2));
console.log(JSON.stringify({ linked }));
