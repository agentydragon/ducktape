// LowerSplit must accept the annotated source before it writes the emitted tree.
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { readFileSync, mkdirSync, writeFileSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const temp = process.env.TEST_TMPDIR;
assert(temp, "TEST_TMPDIR is required");
const source = path.join(here, "fixtures/local_identity/bundle.js");
const code = readFileSync(source, "utf8");
const out = path.join(temp, "emitted");
mkdirSync(out, { recursive: true });
const program = acorn.parse(code, { sourceType: "module", ecmaVersion: "latest", ranges: true });
const statements = program.body;
const ast = path.join(temp, "ast.json");
const spec = path.join(temp, "spec.json");
const bundle = path.join(temp, "bundle.json");
const tree = path.join(temp, "tree.json");
const cert = path.join(temp, "cert.json");
writeFileSync(
  ast,
  JSON.stringify({ statements: statements.map((s) => ({ ast: s, src: code.slice(s.start, s.end) })) })
);
writeFileSync(
  spec,
  JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["identity", "result", "#3"], "side.mjs": ["#2"] } })
);
const run = (bin, args) => spawnSync(bin, args, { encoding: "utf8" });
const raw = run(process.env.LOWERSPLIT, [ast, spec, out]);
assert.equal(raw.status, 1, `raw unannotated input must remain conservative: ${raw.stdout}\n${raw.stderr}`);
assert.match(raw.stdout, /FAIL\s+OrderOk/);
assert(!existsSync(path.join(out, "entry.mjs")), "rejected input must not emit a module");
const lowered = lowerDump(code, acorn, eslintScope).statements;
// Scope annotations must preserve original statements, including multiple
// declarators, imports, exports, and their exact source slices.
const syntax = 'import {x as y} from "./dep.js"; const a = y, b = 1; export {a};';
const syntaxTree = acorn.parse(syntax, { sourceType: "module", ecmaVersion: "latest", ranges: true });
const syntaxStatements = lowerDump(syntax, acorn, eslintScope).statements;
assert.deepEqual(
  syntaxStatements.map((s) => s.ast.type),
  ["ImportDeclaration", "VariableDeclaration", "ExportNamedDeclaration"]
);
assert.equal(syntaxStatements[1].ast.declarations.length, 2);
assert.deepEqual(
  syntaxStatements.map((s) => s.src),
  syntaxTree.body.map((s) => syntax.slice(s.start, s.end))
);
assert.equal(syntaxStatements[1].ast.declarations[0].init.k, "V");
assert.deepEqual(syntaxStatements[1].oracle.eager, ["y"]);
// Removing just the new metadata gives the exact legacy structural JSON.
const legacy = (value) =>
  JSON.parse(
    JSON.stringify(value, function (key, v) {
      if (["start", "end", "loc", "range", "raw", "regex", "bigint", "sourceType"].includes(key)) return undefined;
      if (this?.type === "Literal" && key === "value") return undefined;
      return v;
    })
  );
const unannotate = (value) =>
  JSON.parse(
    JSON.stringify(value, function (key, v) {
      if (this?.type === "Identifier" && ["k", "b", "d"].includes(key)) return undefined;
      if (this?.type === "Literal" && key === "lit") return undefined;
      return v;
    })
  );
assert.deepEqual(
  syntaxStatements.map((s) => unannotate(s.ast)),
  syntaxTree.body.map(legacy)
);
writeFileSync(ast, JSON.stringify({ statements: lowered }));
const leanCertificate = path.join(temp, "generated.lean");
const emitted = run(process.env.LOWERSPLIT, [ast, spec, out, "--lean", leanCertificate]);
assert.equal(emitted.status, 0, `annotated emitter must accept: ${emitted.stdout}\n${emitted.stderr}`);
assert.match(emitted.stdout, /pass\s+OrderOk/);
assert.match(emitted.stdout, /inferred identity call/);
const leanSource = readFileSync(leanCertificate, "utf8");
assert.match(leanSource, /Trusted identity-call inference \(unit, binding\):/);
assert.match(leanSource, /kernel checks this supplied bundle and plan, not the source-to-footprint analysis/);
assert.match(leanSource, /theorem plan_valid/);
const frontend = path.join(here, "frontend.mjs");
for (const args of [
  [frontend, "bundle", source, "--out", bundle],
  [frontend, "tree", out, "--entry", "entry.mjs", "--out", tree],
  [path.join(here, "pair.mjs"), bundle, tree, "--out", cert],
]) {
  const p = run(process.execPath, args);
  assert.equal(p.status, 0, p.stderr);
}
const checked = run(process.env.TRANSLATE, [bundle, tree, cert]);
assert.equal(checked.status, 0, `${checked.stdout}\n${checked.stderr}`);
assert.match(checked.stdout, /inferred identity call/);
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
  const parsed = acorn.parse(unsafe, { sourceType: "module", ecmaVersion: "latest", ranges: true });
  const side = parsed.body.length - 2;
  const unsafeAst = path.join(temp, `${name}.json`);
  const unsafeSpec = path.join(temp, `${name}-spec.json`);
  const unsafeOut = path.join(temp, `${name}-out`);
  mkdirSync(unsafeOut, { recursive: true });
  writeFileSync(unsafeAst, JSON.stringify(lowerDump(unsafe, acorn, eslintScope)));
  writeFileSync(
    unsafeSpec,
    JSON.stringify({
      entry: "entry.mjs",
      modules: {
        "entry.mjs": parsed.body.map((_, i) => `#${i}`).filter((_, i) => i !== side),
        "side.mjs": [`#${side}`],
      },
    })
  );
  const rejected = run(process.env.LOWERSPLIT, [unsafeAst, unsafeSpec, unsafeOut]);
  assert.equal(rejected.status, 1, `${name} must be rejected: ${rejected.stdout}\n${rejected.stderr}`);
  assert.match(rejected.stdout, /FAIL\s+OrderOk/);
  assert(!existsSync(path.join(unsafeOut, "entry.mjs")), `${name} must not emit a module`);
}

// An escaped callback may read the result before a reordered initializer. The
// alias case must also be refused, even though only `alias` is read lazily.
for (const [name, unsafe, side, suppress = true] of [
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
    false,
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
    false,
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
    false,
  ],
]) {
  const dir = path.join(temp, name);
  mkdirSync(dir, { recursive: true });
  const original = path.join(temp, `${name}.mjs`);
  const input = path.join(temp, `${name}-ast.json`);
  const split = path.join(temp, `${name}-spec.json`);
  const sourceBundle = path.join(temp, `${name}-bundle.json`);
  const outputTree = path.join(temp, `${name}-tree.json`);
  const pairing = path.join(temp, `${name}-pairing.json`);
  const parsed = acorn.parse(unsafe, { sourceType: "module", ecmaVersion: "latest" });
  writeFileSync(original, unsafe);
  writeFileSync(input, JSON.stringify(lowerDump(unsafe, acorn, eslintScope)));
  writeFileSync(
    split,
    JSON.stringify({
      entry: "entry.mjs",
      modules: {
        "entry.mjs": parsed.body.map((_, i) => `#${i}`).filter((_, i) => !side.includes(i)),
        "side.mjs": side.map((i) => `#${i}`),
      },
    })
  );
  const rejected = run(process.env.LOWERSPLIT, [input, split, dir]);
  assert.equal(rejected.status, 1, `${name}: ${rejected.stdout}\n${rejected.stderr}`);
  assert.match(rejected.stdout, /FAIL\s+OrderOk/);
  if (suppress) assert.doesNotMatch(rejected.stdout, /inferred identity call/);
  else assert.match(rejected.stdout, /inferred identity call/);
  assert(!existsSync(path.join(dir, "entry.mjs")), name);
  const emitted = run(process.env.LOWERSPLIT, [input, split, dir, "--force"]);
  assert.equal(emitted.status, 1, `${name}: ${emitted.stdout}\n${emitted.stderr}`);
  assert.match(emitted.stdout, /FAIL\s+OrderOk/);
  const before = run(process.execPath, [original]);
  const after = run(process.execPath, [path.join(dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.notEqual(after.status, 0, name);
  assert.match(after.stderr, /ReferenceError/);
  for (const args of [
    [frontend, "bundle", original, "--out", sourceBundle],
    [frontend, "tree", dir, "--entry", "entry.mjs", "--out", outputTree],
    [path.join(here, "pair.mjs"), sourceBundle, outputTree, "--out", pairing],
  ]) {
    const p = run(process.execPath, args);
    assert.equal(p.status, 0, p.stderr);
  }
  const checkedUnsafe = run(process.env.TRANSLATE, [sourceBundle, outputTree, pairing]);
  assert.equal(checkedUnsafe.status, 4, `${name}: ${checkedUnsafe.stdout}\n${checkedUnsafe.stderr}`);
  assert.match(checkedUnsafe.stdout, /FAIL\s+OrderOk/);
  if (suppress) assert.doesNotMatch(checkedUnsafe.stdout, /inferred identity call/);
  else assert.match(checkedUnsafe.stdout, /inferred identity call/);
}

// Export metadata is separate from bundle units, but an external module can
// observe an exported binding. Do not infer the call as inert in that case.
{
  const name = "exported_result";
  const unsafe = `${code}\nexport { result };\n`;
  const dir = path.join(temp, name);
  mkdirSync(dir, { recursive: true });
  const original = path.join(temp, `${name}.mjs`);
  const input = path.join(temp, `${name}-ast.json`);
  const split = path.join(temp, `${name}-spec.json`);
  const sourceBundle = path.join(temp, `${name}-bundle.json`);
  const outputTree = path.join(temp, `${name}-tree.json`);
  const pairing = path.join(temp, `${name}-pairing.json`);
  const parsed = acorn.parse(unsafe, { sourceType: "module", ecmaVersion: "latest" });
  writeFileSync(original, unsafe);
  writeFileSync(input, JSON.stringify(lowerDump(unsafe, acorn, eslintScope)));
  writeFileSync(
    split,
    JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#1", "#3", "#4"], "side.mjs": ["#2"] } })
  );
  const rejected = run(process.env.LOWERSPLIT, [input, split, dir]);
  assert.equal(rejected.status, 1, rejected.stdout);
  assert.match(rejected.stdout, /FAIL\s+OrderOk/);
  assert.doesNotMatch(rejected.stdout, /inferred identity call/);
  assert(!existsSync(path.join(dir, "entry.mjs")));
  const emitted = run(process.env.LOWERSPLIT, [input, split, dir, "--force"]);
  assert.equal(emitted.status, 1, emitted.stdout);
  for (const args of [
    [frontend, "bundle", original, "--out", sourceBundle],
    [frontend, "tree", dir, "--entry", "entry.mjs", "--out", outputTree],
    [path.join(here, "pair.mjs"), sourceBundle, outputTree, "--out", pairing],
  ]) {
    const p = run(process.execPath, args);
    assert.equal(p.status, 0, p.stderr);
  }
  const checkedExport = run(process.env.TRANSLATE, [sourceBundle, outputTree, pairing]);
  assert.equal(checkedExport.status, 4, `${checkedExport.stdout}\n${checkedExport.stderr}`);
  assert.doesNotMatch(checkedExport.stdout, /inferred identity call/);
  assert.match(checkedExport.stdout, /FAIL\s+OrderOk/);
}
