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
