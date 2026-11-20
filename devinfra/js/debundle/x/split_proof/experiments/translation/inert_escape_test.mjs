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
const tests = [
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
