import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";

for (const [file, ordinal] of process.argv.slice(2).map((s) => s.split(":"))) {
  const text = readFileSync(file, "utf8");
  const ast = acorn.parse(text, { ecmaVersion: "latest", sourceType: "module" });
  const unit = topLevelStatements(ast)[Number(ordinal)];
  const labels = [];
  function walk(node) {
    if (!node || typeof node !== "object") return;
    if (node.type === "LabeledStatement") labels.push(node);
    for (const value of Object.values(node)) {
      if (typeof value === "object") Array.isArray(value) ? value.forEach(walk) : walk(value);
    }
  }
  walk(unit.node);
  assert.equal(labels.length, 1);
  // Preserve the original labeled statement byte for byte, but project it out
  // of its enclosing function. This measures lowering, not whole-library execution.
  const source = text.slice(labels[0].start, labels[0].end);
  const parsed = parseModule(source, acorn.parse, eslintScope);
  const snapshot = dumpParsed(parsed, { includeOpaqueCaptures: true });
  const input = path.join(process.env.TEST_TMPDIR, `label-${ordinal}.json`);
  writeFileSync(input, JSON.stringify(snapshot));
  const census = JSON.parse(execFileSync(process.env.COREJS, ["--lowering-census", input], { encoding: "utf8" }));
  assert.deepEqual(census.lowering_refusals, [{ unit: 0, reason: "unsupported global nonASCIIwhitespace" }]);
  console.log(JSON.stringify({ file, ordinal, label: labels[0].label.name, census }));

  // Supply explicit lexical collaborators to exercise the unchanged loop in a
  // small receiver. The corpus module and its actual globals are not modeled.
  for (const codes of [
    [32, 32, 47, 120],
    [32, 10, 88],
  ]) {
    const program = `const nonASCIIwhitespace = { test() { return false; } };
      const String = { fromCharCode(ch) { return ""; } };
      const codes = ${JSON.stringify(codes)};
      function scan() { ${source} }
      const input = { length: ${codes.length}, charCodeAt(i) { return codes[i]; } };
      const p = { pos: 0, input, options: { locations: false }, scan };
      p.scan(); throw p.pos;`;
    let node;
    try {
      await import(`data:text/javascript,${encodeURIComponent(program)}`);
      node = ["normal", ""];
    } catch (e) {
      node = ["throw", `${typeof e}:${e}`];
    }
    const astInput = path.join(process.env.TEST_TMPDIR, "label-program.jsonl");
    writeFileSync(astInput, JSON.stringify(dumpParsed(parseModule(program, acorn.parse, eslintScope))) + "\n");
    const result = JSON.parse(execFileSync(process.env.COREJS, ["--ast", astInput], { encoding: "utf8" }));
    assert.deepEqual([result.end, result.detail], node);
  }
}

// Construct invalid ESTree directly: a source parser normally rejects these
// before the lowerer can check them.
function unit(source) {
  return dumpParsed(parseModule(source, acorn.parse, eslintScope)).body[0];
}
const wrong = unit("outer: while (true) { break outer; }");
wrong.body.body.body[0].label.name = "missing";
const block = unit("outer: while (true) { continue outer; }");
block.body.type = "BlockStatement";
const boundary = unit("outer: while (true) { function f() { inner: while (true) { break inner; } } break outer; }");
boundary.body.body.body[0].body.body[0].body.body.body[0].label.name = "outer";
const duplicate = unit("outer: inner: while (true) { break inner; }");
duplicate.body.label.name = "outer";
const forIn = unit("outer: for (const key in {}) { continue outer; }");
const cases = [wrong, block, boundary, duplicate, forIn].map((input) => ({ kind: "labelUnit", input }));
const check = spawnSync(process.env.LINKED_INPUT_CHECKER, { input: JSON.stringify(cases) + "\n", encoding: "utf8" });
assert.equal(check.status, 0, check.stderr);
assert.deepEqual(
  JSON.parse(check.stdout).map((result) => result.error),
  [
    "unbound control label",
    "continue target is not a supported loop",
    "unbound control label",
    "duplicate active label",
    "continue target is not a supported loop",
  ]
);
