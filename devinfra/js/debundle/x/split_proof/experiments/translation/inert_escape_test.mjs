// Splits that move a statement across something that can observe it. `lowersplit` writes the emitted modules only when
// its checks pass; `translate` then checks the emitted tree against the source on its own.
//
// - `refuse`: a callback or an exported binding can observe the move. Node must run the forced split differently, both
//   checkers must refuse it, and nothing is written without `--force`.
// - `accept`: the move changes nothing. Both checkers must accept it and Node must print the same.
// - `byBehavior`: the checker may go either way, but a split that Node runs differently must be refused.
//
// Not covered, because the checker assumes it does not happen (premise P4: evaluation completes): a source that throws
// when it runs, such as one that reads a `const` before its declaration.
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const temp = process.env.TEST_TMPDIR;
assert(temp);
const here = path.dirname(fileURLToPath(import.meta.url));
const run = (bin, args) => spawnSync(bin, args, { encoding: "utf8" });
const frontend = path.join(here, "frontend.mjs");
const fixture = (name) => readFileSync(path.join(here, "testdata/inert_escape", name), "utf8");

// The source, its annotated ESTree and a split that moves the statements `side` into `side.mjs`.
function prepare(name, code, side) {
  const dir = path.join(temp, name);
  mkdirSync(dir, { recursive: true });
  const parsed = acorn.parse(code, { sourceType: "module", ecmaVersion: "latest" });
  const source = path.join(temp, `${name}.mjs`);
  const input = path.join(temp, `${name}-ast.json`);
  const spec = path.join(temp, `${name}-spec.json`);
  writeFileSync(source, code);
  writeFileSync(input, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  writeFileSync(
    spec,
    JSON.stringify({
      entry: "entry.mjs",
      modules: {
        "entry.mjs": parsed.body.map((_, i) => `#${i}`).filter((_, i) => !side.includes(i)),
        "side.mjs": side.map((i) => `#${i}`),
      },
    })
  );
  return { dir, source, input, spec };
}

// `translate` on the tree in `dir` against the source.
function translate(name, source, dir) {
  const bundle = path.join(temp, `${name}-bundle.json`);
  const tree = path.join(temp, `${name}-tree.json`);
  const cert = path.join(temp, `${name}-cert.json`);
  for (const args of [
    [frontend, "bundle", source, "--out", bundle],
    [frontend, "tree", dir, "--entry", "entry.mjs", "--out", tree],
    [path.join(here, "pair.mjs"), bundle, tree, "--out", cert],
  ]) {
    const result = run(process.execPath, args);
    assert.equal(result.status, 0, result.stderr);
  }
  return run(process.env.TRANSLATE, [bundle, tree, cert]);
}

const lowersplit = (p, ...flags) => run(process.env.LOWERSPLIT, [p.input, p.spec, p.dir, ...flags]);
const describe = (name, r) => `${name}: ${r.stdout}\n${r.stderr}`;

function refuse(name, code, side) {
  const p = prepare(name, code, side);
  const rejected = lowersplit(p);
  assert.equal(rejected.status, 1, describe(name, rejected));
  assert.match(rejected.stdout, /FAIL\s+OrderOk/, name);
  assert(!existsSync(path.join(p.dir, "entry.mjs")), `${name} wrote a module without --force`);
  assert.equal(lowersplit(p, "--force").status, 1, name);
  const before = run(process.execPath, [p.source]);
  const after = run(process.execPath, [path.join(p.dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.notEqual(after.status, 0, `${name}: the forced split should fail`);
  assert.match(after.stderr, /ReferenceError/, name);
  const checked = translate(name, p.source, p.dir);
  assert.equal(checked.status, 4, describe(name, checked));
  assert.match(checked.stdout, /FAIL\s+OrderOk/, name);
}

function accept(name, code, side) {
  const p = prepare(name, code, side);
  const emitted = lowersplit(p);
  assert.equal(emitted.status, 0, describe(name, emitted));
  const before = run(process.execPath, [p.source]);
  const after = run(process.execPath, [path.join(p.dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.equal(after.status, 0, after.stderr);
  assert.equal(after.stdout, before.stdout, name);
  const checked = translate(name, p.source, p.dir);
  assert.equal(checked.status, 0, describe(name, checked));
}

function byBehavior(name, code, side) {
  const p = prepare(name, code, side);
  const unforced = lowersplit(p);
  lowersplit(p, "--force");
  const before = run(process.execPath, [p.source]);
  const after = run(process.execPath, [path.join(p.dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  const differs = after.status !== 0 || after.stdout !== before.stdout;
  const checked = translate(name, p.source, p.dir);
  if (differs) {
    assert.equal(
      unforced.status,
      1,
      `${name}: a split that changes the output was accepted: ${describe(name, unforced)}`
    );
    assert.equal(checked.status, 4, describe(name, checked));
  } else if (unforced.status === 0) {
    assert.equal(checked.status, 0, describe(name, checked));
  }
}

for (const [name, code, side] of [
  ["literal_read_tdz", `const result = 7;\nconst alias = result;\nconsole.log(alias);`, [1, 2]],
  [
    "literal_callback",
    `function f() { return result; }\nglobalThis.saved = f;\nconst result = 7;\nconsole.log(globalThis.saved());`,
    [0, 1, 3],
  ],
  [
    "direct",
    `function f() { return result(); }\nglobalThis.saved = f;\nconst result = () => 7;\nconsole.log(globalThis.saved());`,
    [0, 1, 3],
  ],
  [
    "alias_chain",
    `function f() { return alias2(); }\nglobalThis.saved = f;\nconst result = () => 7;\nconst alias = result;\nconst alias2 = alias;\nconsole.log(globalThis.saved());`,
    [0, 1, 5],
  ],
  [
    "object_alias",
    `function f() { return alias.value(); }\nglobalThis.saved = f;\nconst result = () => 7;\nconst alias = {value: result};\nconsole.log(globalThis.saved());`,
    [0, 1, 4],
  ],
  [
    "array_alias",
    `function f() { return alias[0](); }\nglobalThis.saved = f;\nconst result = () => 7;\nconst alias = [result];\nconsole.log(globalThis.saved());`,
    [0, 1, 4],
  ],
  [
    "mutable_slot",
    `let slot;
function register(f) { slot = f; }
function trigger() { return slot(); }
function later() { return result(); }
register(later);
const result = () => 7;
console.log(trigger());`,
    [0, 1, 2, 3, 4, 6],
  ],
  ["generated_2026_3", fixture("seed2026_round3.js.txt"), [0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 12]],
  ["generated_2026_8", fixture("seed2026_round8.js.txt"), [0, 1, 2, 3, 4, 5, 6, 7, 10, 11]],
  // an escaped callback reads `x`, and the split runs the array's call before `x` is initialized
  [
    "array_reentry_call",
    `globalThis.saved = () => x;\nconst x = 1;\nconst arr = [globalThis.saved()];\nconsole.log(arr[0]);`,
    [0, 2],
  ],
  [
    "array_reentry_spread",
    `globalThis.saved = () => x;\nconst x = 1;\nconst arr = [...{ *[Symbol.iterator]() { yield globalThis.saved(); } }];\nconsole.log(arr[0]);`,
    [0, 2],
  ],
]) {
  refuse(name, code, side);
}

// Exported declarations have no export-specifier read in the input AST, yet an external cyclic importer may observe
// their bindings during evaluation.
{
  const name = "export_declaration";
  const p = prepare(name, `export const local = 7;\nglobalThis.hook();`, [1]);
  const rejected = lowersplit(p);
  assert.equal(rejected.status, 1, describe(name, rejected));
  assert.match(rejected.stdout, /FAIL\s+OrderOk/);
}

// Node control for the external-module cycle: `lowersplit` rejects import syntax, so these source modules exercise the
// ESM behavior being guarded directly.
for (const [name, reordered] of [
  ["cycle_original", false],
  ["cycle_reordered", true],
]) {
  const dir = path.join(temp, name);
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    path.join(dir, "observer.mjs"),
    `import { local } from "./entry.mjs";\nglobalThis.hook = () => console.log(local);`
  );
  writeFileSync(path.join(dir, "side.mjs"), `globalThis.hook();`);
  writeFileSync(
    path.join(dir, "entry.mjs"),
    reordered
      ? `import "./observer.mjs";\nimport "./side.mjs";\nexport const local = 7;`
      : `import "./observer.mjs";\nexport const local = 7;\nglobalThis.hook();`
  );
  const result = run(process.execPath, [path.join(dir, "entry.mjs")]);
  if (reordered) {
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /ReferenceError/);
  } else {
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, "7\n");
  }
}

for (const [name, code, side] of [
  // a parameter that shadows a module binding is not a read of it
  [
    "shadow",
    `function local(result) { return result; }\nconst result = () => 7;\nconsole.log("side");\nconsole.log(result());`,
    [2],
  ],
  // the two declarations initialize in the other order; the callback runs after both
  ["constant_pair", `const a = 1;\nconst b = 2;\nfunction f() { return a + b; }\nconsole.log(f());`, [1]],
  [
    "literal_arrow",
    `const x = 1;\nconst f = () => 2;\nfunction see() { return [x, f]; }\nconsole.log(see()[0], see()[1]());`,
    [1],
  ],
  [
    "literal_record",
    `const x = 1;\nconst o = { color: "v", enabled: true };\nfunction see() { return [x, o]; }\nconsole.log(see()[0], see()[1].color);`,
    [1],
  ],
  [
    "literal_array",
    `const x = 1;\nconst a = ["v",, true];\nfunction see() { return [x, a]; }\nconsole.log(see()[0], see()[1][0], 1 in see()[1]);`,
    [1],
  ],
  // a read of an earlier initialized binding may move across an unrelated effect
  [
    "prior_initialized_read",
    `const value = 1;\nconst alias = value;\nconsole.log("side");\nconsole.log(alias);`,
    [0, 2],
  ],
]) {
  accept(name, code, side);
}

// Judged by what Node prints.
for (const [name, code, side] of [
  [
    "arrow_captures_literal",
    `const x = 1;\nconst f = () => x;\nfunction expose() { return f; }\nconsole.log(expose()());`,
    [1],
  ],
  [
    "two_observed_arrows",
    `const f = () => 1;\nconst g = () => 2;\nfunction see() { return [f, g]; }\nconsole.log(see()[0](), see()[1]());`,
    [1],
  ],
  [
    "two_observed_arrays",
    `const a = [1];\nconst b = [2];\nfunction see() { return [a, b]; }\nconsole.log(see());`,
    [1],
  ],
]) {
  byBehavior(name, code, side);
}
