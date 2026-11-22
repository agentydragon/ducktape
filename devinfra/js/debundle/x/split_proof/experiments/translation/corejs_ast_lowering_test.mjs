import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
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
