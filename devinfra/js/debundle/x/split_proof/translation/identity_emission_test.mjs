// LowerSplit must accept the annotated source before it writes the emitted tree.
import { strict as assert } from "node:assert";
import { readFileSync, mkdirSync, writeFileSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { bundleFromParsed, frontBundle } from "./frontend.mjs";
import { annotateProgram, parseModule, resolveIdentifiers } from "./scope_metadata.mjs";
import { dumpParsed } from "./annotated_estree.mjs";
import { acorn, dump, eslintScope, loadSnapshot } from "./parser.mjs";
import { checkTranslation } from "./testing/check_translation.mjs";
import { writeDeployment } from "./testing/deployment_manifest.mjs";
import { lowerDiagnostic, prepareSideSplit, run } from "./testing/diagnostic_split.mjs";
import { generateInstance } from "./testing/split.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const temp = process.env.TEST_TMPDIR;
assert(temp, "TEST_TMPDIR is required");
const source = path.join(here, "fixtures/local_identity/bundle.js");
const code = readFileSync(source, "utf8");
const out = path.join(temp, "emitted");
mkdirSync(out, { recursive: true });
function legacyDump(parsed) {
  const meaning = resolveIdentifiers(parsed, () => true);
  return JSON.stringify(annotateProgram(parsed, meaning, { legacy: true }));
}
const ast = path.join(temp, "ast.json");
const spec = path.join(temp, "spec.json");
writeFileSync(
  spec,
  JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["identity", "result", "#3"], "side.mjs": ["#2"] } })
);
const parsed = parseModule(code, acorn.parse, eslintScope);
// The transported Program keeps its declaration list, import and export. Lean splits the list later.
const syntax = 'import {x as y} from "./dep.js"; const a = y, b = 1; export {a};';
const syntaxProgram = dump(syntax);
assert.equal(syntaxProgram.sourceText, syntax);
assert.equal(syntaxProgram.sourceType, "module");
assert.deepEqual(
  syntaxProgram.body.map((s) => syntax.slice(s.start, s.end)),
  ['import {x as y} from "./dep.js";', "const a = y, b = 1;", "export {a};"]
);
assert.deepEqual(
  syntaxProgram.body[1].declarations.map((d) => syntax.slice(d.start, d.end)),
  ["a = y", "b = 1"]
);
assert.equal(syntaxProgram.body[1].declarations[0].init.k, "V");
assert.equal(syntaxProgram.body[2].specifiers[0].local.k, "T");
const structure = (value) =>
  JSON.parse(
    JSON.stringify(value, function (key, v) {
      if (["start", "end", "sourceText", "annotationVersion"].includes(key)) return undefined;
      if (this?.type === "Identifier" && ["k", "b", "d"].includes(key)) return undefined;
      if (this?.type === "Literal" && ["lit", "value", "raw"].includes(key)) return undefined;
      return v;
    })
  );
assert.deepEqual(
  structure(syntaxProgram),
  structure(acorn.parse(syntax, { sourceType: "module", ecmaVersion: "latest" }))
);
// Printer fields cross the JSON boundary, including values JSON cannot represent directly.
const printSource = `/*🌱*/ const literals = [/a+/gi, 0xffn, 1e400, -0];
const holder = { get /* keep */ item() { return 1; }, async *items() {} };
export class Thing { static run() {} }
const closures = [x => x, function () {}, class {}];`;
const printParsed = parseModule(printSource, acorn.parse, eslintScope);
const printProgram = JSON.parse(JSON.stringify(dumpParsed(printParsed)));
assert.equal(printProgram.annotationVersion, 2);
const [literalDecl, objectDecl, exportedClass, closuresDecl] = printProgram.body;
assert.deepEqual(literalDecl.declarations[0].init.elements, [
  { type: "Literal", lit: "r:a+/gi", raw: "/a+/gi", value: null, regex: { pattern: "a+", flags: "gi" } },
  { type: "Literal", lit: "bi:255", raw: "0xffn", value: "255", bigint: "255" },
  { type: "Literal", lit: "n:Infinity", raw: "1e400", value: "Infinity" },
  {
    type: "UnaryExpression",
    operator: "-",
    prefix: true,
    argument: { type: "Literal", lit: "n:0", raw: "0", value: 0 },
  },
]);
const methods = objectDecl.declarations[0].init.properties;
const classDecl = exportedClass.declaration;
const closures = closuresDecl.declarations[0].init.elements;
assert.deepEqual(
  [...methods, classDecl, ...classDecl.body.body, ...closures].map(({ start, end }) => printSource.slice(start, end)),
  [
    "get /* keep */ item() { return 1; }",
    "async *items() {}",
    "class Thing { static run() {} }",
    "static run() {}",
    "x => x",
    "function () {}",
    "class {}",
  ]
);
const legacy = JSON.parse(legacyDump(printParsed));
assert.deepEqual(loadSnapshot(legacy), printProgram);
legacy.body[0].declarations[0].init.elements[0].lit = "r:b+/gi";
assert.throws(() => loadSnapshot(legacy), /disagrees with a fresh parse/);
writeFileSync(ast, JSON.stringify(dumpParsed(parsed)));
const scopes = dump("const a = (x) => x, b = (x) => x;");
const callbacks = scopes.body[0].declarations.map((d) => d.init);
assert.notEqual(callbacks[0].params[0].b, callbacks[1].params[0].b);
for (const callback of callbacks) assert.equal(callback.params[0].b, callback.body.b);
const leanCertificate = path.join(temp, "generated.lean");
generateInstance({ ast, spec, out: leanCertificate, preview: path.join(temp, "instance-generator-preview") });
const emitted = run(process.env.SPLIT_PREVIEW, [writeDeployment(ast, spec, out), out]);
assert.equal(emitted.status, 0, `annotated emitter must accept: ${emitted.stdout}\n${emitted.stderr}`);
assert.match(emitted.stdout, /pass\s+OrderOk/);
const legacyInput = path.join(temp, "legacy-ast.json");
writeFileSync(legacyInput, legacyDump(parsed));
const upgradedOut = path.join(temp, "upgraded");
const upgraded = run(process.env.SPLIT_PREVIEW, [writeDeployment(legacyInput, spec, upgradedOut), upgradedOut]);
assert.equal(upgraded.status, 0, `${upgraded.stdout}\n${upgraded.stderr}`);
for (const file of ["entry.mjs", "side.mjs"])
  assert.equal(readFileSync(path.join(upgradedOut, file), "utf8"), readFileSync(path.join(out, file), "utf8"));
const leanSource = readFileSync(leanCertificate, "utf8");
assert.match(leanSource, /kernel checks this supplied bundle and plan, not the source-to-footprint analysis/);
assert.match(leanSource, /theorem plan_valid/);

// A generated instance retains the original spelling of whole statements
// and standalone declarations for units split out of a declaration list.
{
  const source = path.join(temp, "instance-source.mjs");
  const spec = path.join(temp, "instance-spec.json");
  const instance = path.join(temp, "instance.lean");
  const original = "const /* retained */ seed = 1;";
  writeFileSync(source, `${original}\nconst left = seed, right = 2;\n`);
  writeFileSync(spec, JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["seed", "left", "right"] } }));
  generateInstance({ source, spec, out: instance, preview: path.join(temp, "instance-out") });
  const generated = readFileSync(instance, "utf8");
  for (const unit of [original, "const left = seed;", "const right = 2;"])
    assert(generated.includes(`src := ${JSON.stringify(unit)}`), unit);
}

const checked = checkTranslation(bundleFromParsed(parsed), out);
assert.equal(checked.status, 0, `${checked.stdout}\n${checked.stderr}`);
assert.match(checked.stdout, /PLAN SOUND/);
const before = run(process.execPath, [source]);
const after = run(process.execPath, [path.join(out, "entry.mjs")]);
assert.equal(before.status, 0, before.stderr);
assert.equal(after.status, 0, after.stderr);
assert.equal(after.stdout, before.stdout);

// The real emitter must still refuse order changes when the callee body, the
// argument, or a nested reassignment can have effects.
for (const [name, unsafe] of [
  [
    "body",
    `function identity(x) { console.log("body"); return x; }\nconst result = identity(1);\nconsole.log("side");\nconsole.log(result);`,
  ],
  [
    "argument",
    `function identity(x) { return x; }\nconst result = identity((console.log("argument"), 1));\nconsole.log("side");\nconsole.log(result);`,
  ],
  [
    "nested_reassignment",
    `function identity(x) { return x; }\nfunction change() { identity = x => { console.log("changed"); return x; }; }\nchange();\nconst result = identity(1);\nconsole.log("side");\nconsole.log(result);`,
  ],
]) {
  const fixture = prepareSideSplit(name, unsafe);
  const rejected = lowerDiagnostic(fixture);
  assert.equal(rejected.status, 1, `${name} must be rejected: ${rejected.stdout}\n${rejected.stderr}`);
  assert.match(rejected.stdout, /FAIL\s+OrderOk/);
  assert(!existsSync(path.join(fixture.dir, "entry.mjs")), `${name} must not emit a module`);
}

// An escaped callback may read the result before a reordered initializer. The
// alias case must also be refused, even though only `alias` is read lazily.
for (const [name, unsafe, side] of [
  [
    "deferred_result",
    `function identity(x) { return x; }
globalThis.cb = () => result;
const result = identity(1);
globalThis.cb();`,
    [1, 3],
  ],
  [
    "deferred_alias",
    `function identity(x) { return x; }
globalThis.cb = () => alias;
const result = identity(1);
const alias = result;
globalThis.cb();`,
    [1, 4],
  ],
  [
    "deferred_object_alias",
    `function identity(x) { return x; }
globalThis.cb = () => alias.value;
const result = identity(1);
const alias = {value: result};
globalThis.cb();`,
    [1, 4],
  ],
  [
    "deferred_array_alias",
    `function identity(x) { return x; }
globalThis.cb = () => alias[0];
const result = identity(1);
const alias = [result];
globalThis.cb();`,
    [1, 4],
  ],
  [
    "assignment_alias",
    `function identity(x) { return x; }
let alias;
globalThis.cb = () => alias;
const result = identity(1);
alias = result;
globalThis.cb();`,
    [1, 2, 4, 5],
  ],
  [
    "destructuring_alias",
    `function identity(x) { return x; }
let alias;
globalThis.cb = () => alias;
const result = identity(1);
({x: alias} = {x: result});
globalThis.cb();`,
    [1, 2, 4, 5],
  ],
  [
    "effectful_store",
    `function identity(x) { return x; }
function store(value) { globalThis.value = value; }
globalThis.cb = () => globalThis.value;
const result = identity(1);
store(result);
globalThis.cb();`,
    [2, 4, 5],
  ],
]) {
  const fixture = prepareSideSplit(name, unsafe, side);
  const { dir, source: original } = fixture;
  const rejected = lowerDiagnostic(fixture);
  assert.equal(rejected.status, 1, `${name}: ${rejected.stdout}\n${rejected.stderr}`);
  assert.match(rejected.stdout, /FAIL\s+OrderOk/);
  assert(!existsSync(path.join(dir, "entry.mjs")), name);
  const emitted = lowerDiagnostic(fixture, "--force");
  assert.equal(emitted.status, 1, `${name}: ${emitted.stdout}\n${emitted.stderr}`);
  assert.match(emitted.stdout, /FAIL\s+OrderOk/);
  const before = run(process.execPath, [original]);
  const after = run(process.execPath, [path.join(dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.notEqual(after.status, 0, name);
  assert.match(after.stderr, /ReferenceError/);
  const checkedUnsafe = checkTranslation(frontBundle(original), dir);
  assert.equal(checkedUnsafe.status, 4, `${name}: ${checkedUnsafe.stdout}\n${checkedUnsafe.stderr}`);
  assert.match(checkedUnsafe.stdout, /FAIL\s+OrderOk/);
}

// A fallback syntax node in a statement mentioning the result may hide a
// deferred use; the statement is reported with its fallback nodes.
{
  const codeWithFallback = `function identity(x) { return x; }
const result = identity(1);
console.log(import.meta.url, result);`;
  const input = path.join(temp, "fallback-identity-ast.json");
  const spec = path.join(temp, "fallback-identity-spec.json");
  const dir = path.join(temp, "fallback-identity-out");
  mkdirSync(dir, { recursive: true });
  writeFileSync(input, JSON.stringify(dump(codeWithFallback)));
  writeFileSync(spec, JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#1", "#2"] } }));
  const result = run(process.env.SPLIT_PREVIEW, [writeDeployment(input, spec, dir), dir]);
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.match(result.stdout, /1 with fallback nodes/);
}
