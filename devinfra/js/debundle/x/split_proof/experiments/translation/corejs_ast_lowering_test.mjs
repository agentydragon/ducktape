import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../lowering/dump_core.mjs";
import { coreFromParsed } from "../model/to_core.mjs";
import { bundleFromParsed } from "./frontend.mjs";
import { parseModule } from "./scope_metadata.mjs";
import { coreOf, dumpOf } from "./split_dump.mjs";

const dir = process.env.TEST_TMPDIR;
function file(name, contents) {
  const dest = path.join(dir, name);
  writeFileSync(dest, contents);
  return dest;
}

function reference(source) {
  const { ids, tokens, exports } = coreOf(source);
  return { entry: "main", modules: [{ name: "main", imports: [], ids, tokens, exports }] };
}

function run(programs, ast) {
  const input = file(ast ? "asts.jsonl" : "programs.jsonl", programs.map((p) => JSON.stringify(p)).join("\n") + "\n");
  return execFileSync(process.env.COREJS, [...(ast ? ["--ast"] : []), input], { encoding: "utf8" })
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
}

// Each seed builds a fresh module and binding chain. Throwing the last value
// makes precision, shadowing and initialization observable in Node and CoreJS.
const literals = [
  "null",
  "true",
  "false",
  '"a\\nβ"',
  "undefined",
  "NaN",
  "Infinity",
  "5e-324",
  "1.7976931348623157e308",
  "1e400",
];
const sources = [
  "let a; var b; export { a, b };",
  "const before = later; let later = 2;",
  "var before = later; var later = 2; throw before;",
  'const undefined = "shadow"; throw undefined;',
];
for (let seed = 0; seed < 48; seed++) {
  const value = seed % 3 === 0 ? String((seed + 1) * 1.234567891234567e-13) : literals[seed % literals.length];
  const declarations = [`export const first = ${value};`, "; debugger;"];
  let last = "first";
  for (let i = 0; i <= seed % 5; i++) {
    const name = `v${i}`;
    declarations.push(`${["var", "let", "const"][(seed + i) % 3]} ${name} = ${last};`);
    last = name;
  }
  sources.push(`${declarations.join("\n")} export { ${last} as result }; throw ${last};`);
}
const actual = run(sources.map(dumpOf), true);
assert.deepEqual(actual, run(sources.map(reference), false));
for (const [i, source] of sources.entries()) {
  let end = "normal";
  let detail = "";
  try {
    await import(`data:text/javascript,${encodeURIComponent(source)}#${i}`);
  } catch (e) {
    end = "throw";
    detail = e instanceof Error ? e.name : e == null ? String(e) : `${typeof e}:${e}`;
  }
  assert.deepEqual([actual[i].end, actual[i].detail], [end, detail], source);
}

const spec = file("spec.json", JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["a", "b"] } }));
function split(source, args = []) {
  const ast = file("bundle.json", JSON.stringify(dumpOf(source)));
  return execFileSync(process.env.LOWERSPLIT, [ast, spec, dir, ...args], { encoding: "utf8" });
}
const source = "var a = 1, b = a;";
const moves = path.join(dir, "moves.json");
assert.match(split(source, ["--move-report", moves]), /proved slice: order kept/);
const nativeReport = JSON.parse(readFileSync(moves));
assert.equal(nativeReport.footprintsOfSyntax, true);
assert.equal(nativeReport.externallyLowered, 0);
// A different model with the same binding footprint cannot replace the AST.
const sidecar = file("other.json", JSON.stringify(reference("var a = 2, b = a;")));
assert.match(split(source, ["--corejs", sidecar]), /FootprintsOfSyntax: against Lean ESTree lowering/);
const unsupported = "var a = [], b = a;";
assert.match(split(unsupported), /proved slice: none\..*statement 0: unsupported expression ArrayExpression/);
const legacy = file("external.json", JSON.stringify(reference(unsupported)));
assert.match(split(unsupported, ["--corejs", legacy]), /externally lowered statements: 1/);

// External input may supply unsupported arrays, but cannot omit their initialization.
const missing = file("missing.json", JSON.stringify(reference("")));
assert.match(split(unsupported, ["--corejs", missing]), /proved slice: none\..*omits statement 0/);
// A call has no declarations, so footprint agreement alone could mistake its missing unit for an empty block.
file("spec.json", JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["a", "#1"] } }));
assert.match(split("var a = 1; a();", ["--corejs", missing]), /proved slice: none\..*omits statement 1/);

// Gaps in executable bodies still have source ordinals. Vary the width of an
// exported list before a destructuring and an opaque closure so their IDs move.
for (let width = 1; width <= 6; width++) {
  const names = Array.from({ length: width }, (_, i) => `n${i}`);
  const code = `import {bag} from "./dep.mjs";
    export var ${names.map((n, i) => `${n} = ${i}`).join(", ")},
      {value: x = n0} = bag, read = ({v}) => x + v;
    export {x as renamed}; debugger; import "./side.mjs"; read;`;
  const parsed = parseModule(code, acorn.parse, eslintScope);
  const dump = dumpParsed(parsed);
  const tree = bundleFromParsed(parsed, "entry.mjs");
  const core = coreFromParsed(parsed, { external: true });
  const bindings = [...names, "x", "read"];
  assert.equal(dump.statements.length, width + 7);
  assert.equal(core.numbered, dump.statements.length);
  assert.deepEqual(
    dump.statements.slice(1, width + 3).map((s) => s.ast.declaration.declarations.length),
    bindings.map(() => 1)
  );
  assert.deepEqual(
    tree.units.map((n) => n.type),
    [...bindings.map(() => "VariableDeclaration"), "DebuggerStatement", "ExpressionStatement"]
  );
  assert.deepEqual(
    tree.exports.map((s) => s.exported),
    [...bindings, "renamed"]
  );
  assert.deepEqual(
    core.exports.map(([name]) => name),
    [...bindings, "renamed"]
  );
  assert.deepEqual(core.ids, [...bindings.map((_, i) => i + 1), width + 6]);
  assert.deepEqual(
    core.opaque.map((o) => o.id),
    [width + 1, width + 2]
  );
  assert.deepEqual(dump.statements[width + 1].oracle.free.toSorted(), ["bag", "n0"]);
  assert.deepEqual(dump.statements[width + 2].oracle.free, ["x"]);
  // Linked programs take their IDs from the caller's compact table (imports are linked separately).
  const ids = Array.from({ length: width + 5 }, (_, i) => 100 + 2 * i);
  const linked = coreFromParsed(parsed, { ids });
  assert.deepEqual(linked.ids, [...ids.slice(0, bindings.length), ids.at(-1)]);
}
