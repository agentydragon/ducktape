import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const tmp = process.env.TEST_TMPDIR;
assert(tmp);
const here = path.dirname(fileURLToPath(import.meta.url));
const run = (bin, args) => spawnSync(bin, args, { encoding: "utf8" });
const good = `function factory(f) { return async (...args) => f(...args); }
function callback() { console.log("callback"); }
const result = factory(callback);
console.log("side");
result();`;
function emit(name, code, selectedSide, force = false) {
  const dir = path.join(tmp, name);
  mkdirSync(dir, { recursive: true });
  const ast = path.join(dir, "ast.json");
  const spec = path.join(dir, "spec.json");
  const parsed = acorn.parse(code, { sourceType: "module", ecmaVersion: "latest" });
  const side = selectedSide ?? [parsed.body.length - 2];
  writeFileSync(ast, JSON.stringify(lowerDump(code, acorn, eslintScope)));
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
  return {
    dir,
    result: run(process.env.LOWERSPLIT, [
      ast,
      spec,
      dir,
      "--lean",
      path.join(dir, "certificate.lean"),
      ...(force ? ["--force"] : []),
    ]),
  };
}
const goodResult = emit("good", good);
assert.equal(goodResult.result.status, 0, `${goodResult.result.stdout}\n${goodResult.result.stderr}`);
assert.match(goodResult.result.stdout, /inferred closure factory/);
assert.match(readFileSync(path.join(goodResult.dir, "certificate.lean"), "utf8"), /Trusted closure-factory inference/);
const source = path.join(tmp, "factory-source.mjs");
writeFileSync(source, good);
const before = run(process.execPath, [source]);
const after = run(process.execPath, [path.join(goodResult.dir, "entry.mjs")]);
assert.equal(before.status, 0, before.stderr);
assert.equal(after.status, 0, after.stderr);
assert.equal(after.stdout, before.stdout);
assert.equal(after.stdout, "side\ncallback\n");
const frontend = path.join(here, "frontend.mjs");
const bundle = path.join(goodResult.dir, "bundle.json");
const tree = path.join(goodResult.dir, "tree.json");
const cert = path.join(goodResult.dir, "cert.json");
for (const args of [
  [frontend, "bundle", source, "--out", bundle],
  [frontend, "tree", goodResult.dir, "--entry", "entry.mjs", "--out", tree],
  [path.join(here, "pair.mjs"), bundle, tree, "--out", cert],
]) {
  const r = run(process.execPath, args);
  assert.equal(r.status, 0, r.stderr);
}
const checked = run(process.env.TRANSLATE, [bundle, tree, cert]);
assert.equal(checked.status, 0, `${checked.stdout}\n${checked.stderr}`);
assert.match(checked.stdout, /inferred closure factory/);
const multi = good
  .replace("const result = factory(callback);", "const result = factory(callback);\nconst result2 = factory(callback);")
  .replace("result();", "void (result(), result2());");
const multiResult = emit("multi", multi);
assert.equal(multiResult.result.status, 0, `${multiResult.result.stdout}\n${multiResult.result.stderr}`);
assert.equal((multiResult.result.stdout.match(/inferred closure factory/g) ?? []).length, 2);
const multiSource = path.join(tmp, "multi-source.mjs");
writeFileSync(multiSource, multi);
const multiBefore = run(process.execPath, [multiSource]);
const multiAfter = run(process.execPath, [path.join(multiResult.dir, "entry.mjs")]);
assert.equal(multiBefore.status, 0, multiBefore.stderr);
assert.equal(multiAfter.status, 0, multiAfter.stderr);
assert.equal(multiAfter.stdout, multiBefore.stdout);
assert.equal(multiAfter.stdout, "side\ncallback\ncallback\n");
// An external observer receives the actual fresh closure and invokes it later.
const escape = `globalThis.capture = f => { globalThis.saved = f; };\n${good.replace("result();", "globalThis.capture(result);\nglobalThis.saved();")}`;
const escapeResult = emit("escape", escape, [0, 4]);
assert.equal(escapeResult.result.status, 0, `${escapeResult.result.stdout}\n${escapeResult.result.stderr}`);
const escapeSource = path.join(tmp, "escape-source.mjs");
writeFileSync(escapeSource, escape);
const escapeBefore = run(process.execPath, [escapeSource]);
const escapeAfter = run(process.execPath, [path.join(escapeResult.dir, "entry.mjs")]);
assert.equal(escapeBefore.status, 0, escapeBefore.stderr);
assert.equal(escapeAfter.status, 0, escapeAfter.stderr);
assert.equal(escapeAfter.stdout, escapeBefore.stdout);
assert.equal(escapeAfter.stdout, "side\ncallback\n");
// Moving an observer/call before the result exists must be rejected.
for (const observer of ["result();", "globalThis.observe(result);"]) {
  const bad = good.replace('console.log("side");', observer);
  const { dir, result } = emit(`observer${observer.length}`, bad);
  assert.notEqual(result.status, 0, `${observer}: ${result.stdout}\n${result.stderr}`);
  assert(!existsSync(path.join(dir, "entry.mjs")), observer);
}
for (const [name, bad] of [
  ["body", good.replace("return async", 'console.log("body"); return async')],
  ["argument", good.replace("factory(callback)", 'factory((console.log("argument"), callback))')],
  ["default", good.replace("factory(f)", 'factory(f = console.log("default"))')],
  ["destructure", good.replace("factory(f)", "factory({f})")],
  ["reassign", good.replace("const result", 'factory = f => { console.log("changed"); return f; };\nconst result')],
  ["eval", good.replace("const result", 'eval("factory = f => f");\nconst result')],
  ["callback_reassign", good.replace("const result", 'callback = () => console.log("changed");\nconst result')],
  ["callback_destructure", good.replace("const result", '[callback] = [() => console.log("changed")];\nconst result')],
  [
    "callback_for_of",
    good.replace("const result", 'for (callback of [() => console.log("changed")]) {}\nconst result'),
  ],
  ["callback_for_in", good.replace("const result", "for (callback in {changed: 1}) {}\nconst result")],
  ["callback_update", good.replace("const result", "callback++;\nconst result")],
  ["callback_nested_write", good.replace("const result", "function change() { callback = () => {}; }\nconst result")],
  [
    "callback_default_write",
    good.replace("const result", "function change(x = (callback = () => {})) { let callback; }\nconst result"),
  ],
  [
    "callback_after_inner_shadow",
    good.replace("const result", "function change() { { let callback; } callback = () => {}; }\nconst result"),
  ],
  [
    "eager_callback",
    good.replace("return async (...args) => f(...args)", "return (f(), async (...args) => f(...args))"),
  ],
  [
    "returned_eager_call",
    good.replace(
      "return async (...args) => f(...args)",
      'return (console.log("eager"), async (...args) => f(...args))'
    ),
  ],
  [
    "tdz",
    good.replace('function callback() { console.log("callback"); }', 'const callback = () => console.log("callback");'),
  ],
]) {
  const { dir, result } = emit(name, bad);
  assert.equal(result.status, 1, `${name}: ${result.stdout}\n${result.stderr}`);
  assert.match(result.stdout, /FAIL\s+OrderOk/, name);
  assert(!existsSync(path.join(dir, "entry.mjs")), name);
}

// A callback saved before the factory call may reach its result later through
// host state, a module-local mutable cell, or an inert alias.
const base = `function callback() {}
function factory(f) { return () => f(); }
`;
for (const [name, code, side] of [
  [
    "deferred_result",
    `${base}globalThis.cb = () => result();
const result = factory(callback);
globalThis.cb();`,
    [2, 4],
  ],
  [
    "deferred_alias",
    `${base}globalThis.cb = () => alias();
const result = factory(callback);
const alias = result;
globalThis.cb();`,
    [2, 5],
  ],
  [
    "deferred_object_alias",
    `${base}globalThis.cb = () => alias.value();
const result = factory(callback);
const alias = {value: result};
globalThis.cb();`,
    [2, 5],
  ],
  [
    "instance_field",
    `${base}globalThis.Klass = class { x = result(); };
const result = factory(callback);
new globalThis.Klass();`,
    [2, 4],
  ],
  [
    "mutable_slot",
    `let slot;
function register(f) { slot = f; }
function trigger() { slot(); }
function callback() {}
function factory(f) { return () => f(); }
function later() { result(); }
register(later);
const result = factory(callback);
trigger();`,
    [0, 1, 2, 6, 8],
  ],
]) {
  const rejected = emit(name, code, side);
  assert.equal(rejected.result.status, 1, `${name}: ${rejected.result.stdout}\n${rejected.result.stderr}`);
  assert.match(rejected.result.stdout, /FAIL\s+OrderOk/, name);
  assert.doesNotMatch(rejected.result.stdout, /inferred closure factory/, name);
  assert(!existsSync(path.join(rejected.dir, "entry.mjs")), name);
  const sourceFile = path.join(tmp, `${name}-source.mjs`);
  writeFileSync(sourceFile, code);
  const forced = emit(`${name}-forced`, code, side, true);
  assert.equal(forced.result.status, 1, name);
  const original = run(process.execPath, [sourceFile]);
  const emitted = run(process.execPath, [path.join(forced.dir, "entry.mjs")]);
  assert.equal(original.status, 0, original.stderr);
  assert.notEqual(emitted.status, 0, name);
  assert.match(emitted.stderr, /ReferenceError/, name);
  const originalBundle = path.join(tmp, `${name}-bundle.json`);
  const outputTree = path.join(tmp, `${name}-tree.json`);
  const pairing = path.join(tmp, `${name}-pairing.json`);
  for (const args of [
    [frontend, "bundle", sourceFile, "--out", originalBundle],
    [frontend, "tree", forced.dir, "--entry", "entry.mjs", "--out", outputTree],
    [path.join(here, "pair.mjs"), originalBundle, outputTree, "--out", pairing],
  ]) {
    const r = run(process.execPath, args);
    assert.equal(r.status, 0, r.stderr);
  }
  const checkedForced = run(process.env.TRANSLATE, [originalBundle, outputTree, pairing]);
  assert.equal(checkedForced.status, 4, `${name}: ${checkedForced.stdout}\n${checkedForced.stderr}`);
  assert.match(checkedForced.stdout, /FAIL\s+OrderOk/, name);
  assert.doesNotMatch(checkedForced.stdout, /inferred closure factory/, name);
}

// Frontend exports are stored outside the unit list, so the translator must
// explicitly treat the result as externally observable.
{
  const code = `${good}\nexport { result };\n`;
  const rejected = emit("exported", code, [3]);
  assert.equal(rejected.result.status, 1, rejected.result.stdout);
  assert.match(rejected.result.stdout, /FAIL\s+OrderOk/);
  assert.doesNotMatch(rejected.result.stdout, /inferred closure factory/);
  const sourceFile = path.join(tmp, "factory-export-source.mjs");
  writeFileSync(sourceFile, code);
  const forced = emit("exported-forced", code, [3], true);
  assert.equal(forced.result.status, 1, forced.result.stdout);
  const originalBundle = path.join(tmp, "factory-export-bundle.json");
  const outputTree = path.join(tmp, "factory-export-tree.json");
  const pairing = path.join(tmp, "factory-export-pairing.json");
  for (const args of [
    [frontend, "bundle", sourceFile, "--out", originalBundle],
    [frontend, "tree", forced.dir, "--entry", "entry.mjs", "--out", outputTree],
    [path.join(here, "pair.mjs"), originalBundle, outputTree, "--out", pairing],
  ]) {
    const r = run(process.execPath, args);
    assert.equal(r.status, 0, r.stderr);
  }
  const checkedExport = run(process.env.TRANSLATE, [originalBundle, outputTree, pairing]);
  assert.equal(checkedExport.status, 4, checkedExport.stdout);
  assert.doesNotMatch(checkedExport.stdout, /inferred closure factory/);
  assert.match(checkedExport.stdout, /FAIL\s+OrderOk/);
}
