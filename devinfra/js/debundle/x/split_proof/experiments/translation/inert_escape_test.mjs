import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const temp = process.env.TEST_TMPDIR;
assert(temp);
const here = path.dirname(fileURLToPath(import.meta.url));
const run = (bin, args) => spawnSync(bin, args, { encoding: "utf8" });
const frontend = path.join(here, "frontend.mjs");
const fixture = (name) => readFileSync(path.join(here, "testdata/inert_escape", name), "utf8");
const tests = [
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
];
for (const [name, code, side] of tests) {
  const dir = path.join(temp, name);
  mkdirSync(dir, { recursive: true });
  const source = path.join(temp, `${name}.mjs`);
  const input = path.join(temp, `${name}-ast.json`);
  const spec = path.join(temp, `${name}-spec.json`);
  const bundle = path.join(temp, `${name}-bundle.json`);
  const tree = path.join(temp, `${name}-tree.json`);
  const cert = path.join(temp, `${name}-cert.json`);
  const parsed = acorn.parse(code, { sourceType: "module", ecmaVersion: "latest" });
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
  const rejected = run(process.env.LOWERSPLIT, [input, spec, dir]);
  assert.equal(rejected.status, 1, `${name}: ${rejected.stdout}\n${rejected.stderr}`);
  assert.match(rejected.stdout, /FAIL\s+OrderOk/, name);
  assert(!existsSync(path.join(dir, "entry.mjs")), name);
  const forced = run(process.env.LOWERSPLIT, [input, spec, dir, "--force"]);
  assert.equal(forced.status, 1, name);
  const before = run(process.execPath, [source]);
  const after = run(process.execPath, [path.join(dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.notEqual(after.status, 0, name);
  assert.match(after.stderr, /ReferenceError/, name);
  for (const args of [
    [frontend, "bundle", source, "--out", bundle],
    [frontend, "tree", dir, "--entry", "entry.mjs", "--out", tree],
    [path.join(here, "pair.mjs"), bundle, tree, "--out", cert],
  ]) {
    const result = run(process.execPath, args);
    assert.equal(result.status, 0, result.stderr);
  }
  const checked = run(process.env.TRANSLATE, [bundle, tree, cert]);
  assert.equal(checked.status, 4, `${name}: ${checked.stdout}\n${checked.stderr}`);
  assert.match(checked.stdout, /FAIL\s+OrderOk/, name);
}
// Shadowed local identifiers do not expose the unrelated module binding.
{
  const code = `function local(result) { return result; }\nconst result = () => 7;\nconsole.log("side");\nconsole.log(result());`;
  const dir = path.join(temp, "shadow");
  mkdirSync(dir, { recursive: true });
  const input = path.join(temp, "shadow-ast.json");
  const spec = path.join(temp, "shadow-spec.json");
  const source = path.join(temp, "shadow.mjs");
  writeFileSync(source, code);
  writeFileSync(input, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  writeFileSync(
    spec,
    JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#1", "#3"], "side.mjs": ["#2"] } })
  );
  const emitted = run(process.env.LOWERSPLIT, [input, spec, dir]);
  assert.equal(emitted.status, 0, `${emitted.stdout}\n${emitted.stderr}`);
  const before = run(process.execPath, [source]);
  const after = run(process.execPath, [path.join(dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.equal(after.status, 0, after.stderr);
  assert.equal(after.stdout, before.stdout);
}

// The returned callback makes both literal bindings observable later. The split
// reverses only their initialization, while leaving the callback after both.
{
  const code = `const a = 1;\nconst b = 2;\nfunction f() { return a + b; }\nconsole.log(f());`;
  const dir = path.join(temp, "constant_pair");
  mkdirSync(dir, { recursive: true });
  const source = path.join(temp, "constant_pair.mjs");
  const input = path.join(temp, "constant_pair-ast.json");
  const spec = path.join(temp, "constant_pair-spec.json");
  const certificate = path.join(temp, "constant_pair-generated.lean");
  writeFileSync(source, code);
  writeFileSync(input, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  writeFileSync(
    spec,
    JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#2", "#3"], "side.mjs": ["#1"] } })
  );
  const emitted = run(process.env.LOWERSPLIT, [input, spec, dir, "--lean", certificate]);
  assert.equal(emitted.status, 0, `${emitted.stdout}\n${emitted.stderr}`);
  assert.match(readFileSync(certificate, "utf8"), /observedInit := true/);
  const before = run(process.execPath, [source]);
  const after = run(process.execPath, [path.join(dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.equal(after.status, 0, after.stderr);
  assert.equal(after.stdout, before.stdout);
  const bundle = path.join(temp, "constant_pair-bundle.json");
  const tree = path.join(temp, "constant_pair-tree.json");
  const cert = path.join(temp, "constant_pair-cert.json");
  for (const args of [
    [frontend, "bundle", source, "--out", bundle],
    [frontend, "tree", dir, "--entry", "entry.mjs", "--out", tree],
    [path.join(here, "pair.mjs"), bundle, tree, "--out", cert],
  ]) {
    const result = run(process.execPath, args);
    assert.equal(result.status, 0, result.stderr);
  }
  const checked = run(process.env.TRANSLATE, [bundle, tree, cert]);
  assert.equal(checked.status, 0, `${checked.stdout}\n${checked.stderr}`);
}

// The normalized frontend records RegExp and BigInt kinds in `lit`, not the
// original ESTree fields. Neither is eligible for primitive-constant inference.
for (const [name, initializer] of [
  ["regexp", "/x/"],
  ["bigint", "1n"],
]) {
  const code = `const a = ${initializer};\nconst b = 2;\nfunction f() { return [a, b]; }\nconsole.log(f());`;
  const dir = path.join(temp, name);
  mkdirSync(dir, { recursive: true });
  const input = path.join(temp, `${name}-ast.json`);
  const spec = path.join(temp, `${name}-spec.json`);
  writeFileSync(input, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  writeFileSync(
    spec,
    JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#2", "#3"], "side.mjs": ["#1"] } })
  );
  const rejected = run(process.env.LOWERSPLIT, [input, spec, dir]);
  assert.equal(rejected.status, 1, `${rejected.stdout}\n${rejected.stderr}`);
  assert.match(rejected.stdout, /FAIL\s+OrderOk/);
}

// Exported declarations have no export-specifier read in the input AST, yet
// an external cyclic importer may observe their bindings during evaluation.
{
  const code = `export const local = 7;\nglobalThis.hook();`;
  const dir = path.join(temp, "export_declaration");
  mkdirSync(dir, { recursive: true });
  const input = path.join(temp, "export_declaration-ast.json");
  const spec = path.join(temp, "export_declaration-spec.json");
  writeFileSync(input, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  writeFileSync(spec, JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#0"], "side.mjs": ["#1"] } }));
  const rejected = run(process.env.LOWERSPLIT, [input, spec, dir]);
  assert.equal(rejected.status, 1, `${rejected.stdout}\n${rejected.stderr}`);
  assert.match(rejected.stdout, /FAIL\s+OrderOk/);
}

// Node control for the external-module cycle. LowerSplit rejects import syntax,
// so these source modules directly exercise the ESM behavior being guarded.
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

// A literal may cross allocation of an independent arrow, but neither the
// closure's identity nor its deferred body is treated as a primitive value.
{
  const code = `const x = 1;\nconst f = () => 2;\nfunction see() { return [x, f]; }\nconsole.log(see()[0], see()[1]());`;
  const dir = path.join(temp, "literal_arrow");
  mkdirSync(dir, { recursive: true });
  const source = path.join(temp, "literal_arrow.mjs");
  const input = path.join(temp, "literal_arrow-ast.json");
  const spec = path.join(temp, "literal_arrow-spec.json");
  const certificate = path.join(temp, "literal_arrow-generated.lean");
  writeFileSync(source, code);
  writeFileSync(input, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  writeFileSync(
    spec,
    JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#2", "#3"], "side.mjs": ["#1"] } })
  );
  const emitted = run(process.env.LOWERSPLIT, [input, spec, dir, "--lean", certificate]);
  assert.equal(emitted.status, 0, `${emitted.stdout}\n${emitted.stderr}`);
  const generated = readFileSync(certificate, "utf8");
  assert.match(generated, /nonReentrant := true/);
  assert.match(generated, /observedInit := true/);
  const before = run(process.execPath, [source]);
  const after = run(process.execPath, [path.join(dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.equal(after.status, 0, after.stderr);
  assert.equal(after.stdout, before.stdout);
  const bundle = path.join(temp, "literal_arrow-bundle.json");
  const tree = path.join(temp, "literal_arrow-tree.json");
  const cert = path.join(temp, "literal_arrow-cert.json");
  for (const args of [
    [frontend, "bundle", source, "--out", bundle],
    [frontend, "tree", dir, "--entry", "entry.mjs", "--out", tree],
    [path.join(here, "pair.mjs"), bundle, tree, "--out", cert],
  ]) {
    const result = run(process.execPath, args);
    assert.equal(result.status, 0, result.stderr);
  }
  const checked = run(process.env.TRANSLATE, [bundle, tree, cert]);
  assert.equal(checked.status, 0, `${checked.stdout}\n${checked.stderr}`);
}

// A closure which captures the moved binding still has a read dependency.
{
  const code = `const x = 1;\nconst f = () => x;\nfunction expose() { return f; }\nconsole.log(expose()());`;
  const dir = path.join(temp, "arrow_captures_literal");
  mkdirSync(dir, { recursive: true });
  const input = path.join(temp, "arrow_captures_literal-ast.json");
  const spec = path.join(temp, "arrow_captures_literal-spec.json");
  writeFileSync(input, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  writeFileSync(
    spec,
    JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#2", "#3"], "side.mjs": ["#1"] } })
  );
  const result = run(process.env.LOWERSPLIT, [input, spec, dir]);
  assert.equal(result.status, 1, `${result.stdout}\n${result.stderr}`);
  assert.match(result.stdout, /FAIL\s+OrderOk/);
}

// Initializer calls, assignment, destructuring and parameter defaults must not
// inherit the direct-arrow classification from a nested function expression.
for (const [name, code] of [
  ["default_param", `const f = (x = 1) => x;\nconsole.log(f());`],
  ["destructured_param", `const f = ({x}) => x;\nconsole.log(f({x:1}));`],
  ["destructure", `const { f = () => 1 } = {};\nconsole.log(f());`],
  ["assignment", `let f;\nf = () => 1;\nconsole.log(f());`],
  ["computed", `const obj = { [String(1)]: () => 1 };\nconsole.log(obj[1]());`],
  ["call", `const f = ((x = 1) => x)();\nconsole.log(f);`],
]) {
  const dir = path.join(temp, `arrow_shape_${name}`);
  mkdirSync(dir, { recursive: true });
  const input = path.join(temp, `arrow_shape_${name}-ast.json`);
  const spec = path.join(temp, `arrow_shape_${name}-spec.json`);
  const certificate = path.join(temp, `arrow_shape_${name}-generated.lean`);
  const parsed = acorn.parse(code, { sourceType: "module", ecmaVersion: "latest" });
  writeFileSync(input, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  writeFileSync(
    spec,
    JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": parsed.body.map((_, i) => `#${i}`) } })
  );
  const result = run(process.env.LOWERSPLIT, [input, spec, dir, "--lean", certificate]);
  assert.equal(result.status, 0, `${name}: ${result.stdout}\n${result.stderr}`);
  assert.doesNotMatch(readFileSync(certificate, "utf8"), /nonReentrant := true/, name);
}

// Two allocations remain ordered so the model never equates fresh identities.
{
  const code = `const f = () => 1;\nconst g = () => 2;\nfunction see() { return [f, g]; }\nconsole.log(see()[0](), see()[1]());`;
  const dir = path.join(temp, "two_observed_arrows");
  mkdirSync(dir, { recursive: true });
  const input = path.join(temp, "two_observed_arrows-ast.json");
  const spec = path.join(temp, "two_observed_arrows-spec.json");
  writeFileSync(input, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  writeFileSync(
    spec,
    JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#2", "#3"], "side.mjs": ["#1"] } })
  );
  const result = run(process.env.LOWERSPLIT, [input, spec, dir]);
  assert.equal(result.status, 1, `${result.stdout}\n${result.stderr}`);
  assert.match(result.stdout, /FAIL\s+OrderOk/);
}

// Identifier values can throw before the later literal initializes. A callback
// that escaped earlier distinguishes the original from the forced bad split,
// even though no code reenters while the object initializer itself runs.
{
  const code = `globalThis.saved = () => x;\nconst x = 1;\nconst obj = { value: later };\nconst later = 2;`;
  const dir = path.join(temp, "record_tdz_observer");
  mkdirSync(dir, { recursive: true });
  const source = path.join(temp, "record_tdz_observer.mjs");
  const input = path.join(temp, "record_tdz_observer-ast.json");
  const spec = path.join(temp, "record_tdz_observer-spec.json");
  writeFileSync(source, code);
  writeFileSync(input, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  writeFileSync(
    spec,
    JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#1", "#3"], "side.mjs": ["#0", "#2"] } })
  );
  const rejected = run(process.env.LOWERSPLIT, [input, spec, dir]);
  assert.equal(rejected.status, 1, `${rejected.stdout}\n${rejected.stderr}`);
  assert.match(rejected.stdout, /FAIL\s+OrderOk/);
  const forced = run(process.env.LOWERSPLIT, [input, spec, dir, "--force"]);
  assert.equal(forced.status, 1, `${forced.stdout}\n${forced.stderr}`);
  const observe = (file) =>
    run(process.execPath, [
      "--input-type=module",
      "-e",
      `try { await import(${JSON.stringify(pathToFileURL(file).href)}); } catch (_) { try { console.log(globalThis.saved()); } catch (error) { console.log(error.name); } }`,
    ]);
  const original = observe(source);
  const split = observe(path.join(dir, "entry.mjs"));
  assert.equal(original.status, 0, original.stderr);
  assert.equal(split.status, 0, split.stderr);
  assert.equal(original.stdout, "1\n");
  assert.equal(split.stdout, "ReferenceError\n");
  const bundle = path.join(temp, "record_tdz_observer-bundle.json");
  const tree = path.join(temp, "record_tdz_observer-tree.json");
  const cert = path.join(temp, "record_tdz_observer-cert.json");
  for (const args of [
    [frontend, "bundle", source, "--out", bundle],
    [frontend, "tree", dir, "--entry", "entry.mjs", "--out", tree],
    [path.join(here, "pair.mjs"), bundle, tree, "--out", cert],
  ]) {
    const result = run(process.execPath, args);
    assert.equal(result.status, 0, result.stderr);
  }
  const checked = run(process.env.TRANSLATE, [bundle, tree, cert]);
  assert.equal(checked.status, 4, `${checked.stdout}\n${checked.stderr}`);
  assert.match(checked.stdout, /FAIL\s+OrderOk/);
}

// A read of an earlier initialized binding can still move across an unrelated
// effect; the output must retain the binding-before-read dependency.
{
  const code = `const value = 1;\nconst alias = value;\nconsole.log("side");\nconsole.log(alias);`;
  const dir = path.join(temp, "prior_initialized_read");
  mkdirSync(dir, { recursive: true });
  const source = path.join(temp, "prior_initialized_read.mjs");
  const input = path.join(temp, "prior_initialized_read-ast.json");
  const spec = path.join(temp, "prior_initialized_read-spec.json");
  writeFileSync(source, code);
  writeFileSync(input, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  writeFileSync(
    spec,
    JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#1", "#3"], "side.mjs": ["#0", "#2"] } })
  );
  const emitted = run(process.env.LOWERSPLIT, [input, spec, dir]);
  assert.equal(emitted.status, 0, `${emitted.stdout}\n${emitted.stderr}`);
  const before = run(process.execPath, [source]);
  const after = run(process.execPath, [path.join(dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.equal(after.status, 0, after.stderr);
  assert.equal(after.stdout, before.stdout);
}
