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
  // A later initializer observes either the preceding binding, a hoisted var,
  // or an uninitialized lexical binding in this same declaration list.
  const group = Array.from(
    { length: 2 + (seed % 5) },
    (_, i) => `part${i} = ${i === 0 ? (seed % 2 === 0 ? value : "part1") : `part${i - 1}`}`
  );
  sources.push(`export ${["var", "let", "const"][seed % 3]} ${group.join(", ")}; throw part1;`);
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
// The checker obtains identities from Program.body even when presented with a
// conflicting legacy table. A table on its own cannot stand in for a module.
const program = dumpOf(source);
assert.equal(program.type, "Program");
assert.equal(program.body.length, 1);
assert.equal(program.body[0].declarations.length, 2);
const [fromBody, legacyOnly] = run([{ ...program, statements: [] }, { statements: [] }], true);
assert.deepEqual(fromBody, run([reference(source)], false)[0]);
assert.equal(legacyOnly.end, "malformed");

// Acorn ranges count UTF-16 code units. Exercise both a supplementary
// character before the declaration and one inside the first declarator.
split('/*😀*/ export var a = "🌱", b = a;');
const rendered = readFileSync(path.join(dir, "entry.mjs"), "utf8");
assert.match(rendered, /export var a = "🌱";/);
assert.match(rendered, /export var b = a;/);
const badRange = dumpOf("/*😀*/ var a = 1, b = a;");
badRange.body[0].declarations[0].start = 3;
const invalid = file("bad-range.json", JSON.stringify(badRange));
assert.throws(() => execFileSync(process.env.LOWERSPLIT, [invalid, spec, dir], { stdio: "pipe" }), /surrogate pair/);
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

// Use both forms of the Lean placement spec. For each generated declaration list,
// keep source order in one plan and move its final independent binding in the other.
for (let width = 2; width <= 4; width++) {
  const names = Array.from({ length: width }, (_, i) => `d${i}`);
  const declarations = names.map((name, i) => `${name} = ${width + i}`).join(", ");
  const code = `${["let", "const", "var"][width - 2]} ${declarations};`;
  for (const moved of [0, width - 1]) {
    const modules = {
      "side.mjs": [names[moved]],
      "entry.mjs": names.flatMap((_, i) => (i === moved ? [] : [`#${i}`])),
    };
    file("spec.json", JSON.stringify({ entry: "entry.mjs", modules }));
    const log = split(code);
    assert.match(log, /Declaration extraction: unsplit declaration lists and the checked CoreJS plan agree/);
    assert.match(log, moved === 0 ? /all modeled outcomes/ : /normally terminating executions/);
  }
}
file("spec.json", JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["a", "b"] } }));
assert.match(split("var a; let b;"), /Declaration extraction: .*all modeled outcomes/);
assert.match(split("export let a = b, b = 1;"), /Declaration extraction: .*all modeled outcomes/);
assert.doesNotMatch(split(unsupported, ["--corejs", legacy]), /Declaration extraction:/);

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
  const tree = bundleFromParsed(parsed);
  const core = coreFromParsed(parsed, { external: true });
  const bindings = [...names, "x", "read"];
  assert.equal(dump.body.length, 6);
  assert.equal(core.numbered, width + 7);
  const declarators = dump.body[1].declaration.declarations;
  assert.deepEqual(
    declarators.map((d) => d.id.type),
    [...names.map(() => "Identifier"), "ObjectPattern", "Identifier"]
  );
  assert.deepEqual(tree.program, dump);
  assert.deepEqual(
    core.exports.map(([name]) => name),
    [...bindings, "renamed"]
  );
  assert.deepEqual(core.ids, [...bindings.map((_, i) => i + 1), width + 6]);
  assert.deepEqual(
    core.opaque.map((o) => o.id),
    [width + 1, width + 2]
  );
  const destructuring = declarators[width];
  assert.equal(destructuring.init.k, "V");
  assert.equal(destructuring.id.properties[0].value.right.k, "T");
  const closure = declarators[width + 1].init;
  assert.equal(closure.body.left.k, "T");
  assert.equal(closure.body.right.k, "L");
  assert.equal(closure.body.right.b, closure.params[0].properties[0].value.b);
  // Linked programs take their IDs from the caller's compact table (imports are linked separately).
  const ids = Array.from({ length: width + 5 }, (_, i) => 100 + 2 * i);
  const linked = coreFromParsed(parsed, { ids });
  assert.deepEqual(linked.ids, [...ids.slice(0, bindings.length), ids.at(-1)]);
}
