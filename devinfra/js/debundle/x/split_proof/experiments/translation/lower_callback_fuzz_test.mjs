// Deterministic generated cases through the actual LowerSplit emitter. Unsafe
// plans must be rejected normally and their force-emitted output must diverge.
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const tmp = process.env.TEST_TMPDIR;
assert(tmp);
const run = (bin, args) => spawnSync(bin, args, { encoding: "utf8" });
function emit(name, code, side, force = false) {
  const dir = path.join(tmp, name);
  mkdirSync(dir, { recursive: true });
  const ast = path.join(dir, "ast.json");
  const spec = path.join(dir, "spec.json");
  const parsed = acorn.parse(code, { sourceType: "module", ecmaVersion: "latest" });
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
  return { dir, result: run(process.env.LOWERSPLIT, [ast, spec, dir, ...(force ? ["--force"] : [])]) };
}

// Fixed seed and stable PRNG make every failing case reproducible.
let seed = 0x91e10da5;
function next() {
  seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
  return seed;
}
for (let i = 0; i < 8; i++) {
  const value = next() % 100000;
  const call = i % 2 ? "return () => { const captured = f; return captured(); };" : "return () => f();";
  const extra = i >= 4 ? `function spare_${i}() { return ${value}; }\n` : "";
  const base = `function callback() { console.log(${value}); }\nfunction factory(f) { ${call} }\n${extra}`;
  const shift = extra ? 1 : 0;
  const rename = (code) => code.replace(/\b(callback|factory|result|alias)\b/g, (name) => `${name}_${i}`);
  const safe = rename(`${base}const result = factory(callback);\nconsole.log("side-${value}");\nresult();`);
  const source = path.join(tmp, `safe-${i}.mjs`);
  writeFileSync(source, safe);
  const emitted = emit(`safe-${i}`, safe, [3 + shift]);
  assert.equal(emitted.result.status, 0, `${i}: ${emitted.result.stdout}\n${emitted.result.stderr}`);
  assert.match(emitted.result.stdout, /inferred closure factory/, `safe ${i}`);
  const original = run(process.execPath, [source]);
  const split = run(process.execPath, [path.join(emitted.dir, "entry.mjs")]);
  assert.equal(original.status, 0, original.stderr);
  assert.equal(split.status, 0, split.stderr);
  assert.equal(split.stdout, original.stdout);
  assert.equal(original.stdout, `side-${value}\n${value}\n`);

  const forms = [
    [`globalThis.cb = () => result();\nconst result = factory(callback);\nglobalThis.cb();`, [2, 4]],
    [
      `globalThis.cb = () => alias();\nconst result = factory(callback);\nconst alias = result;\nglobalThis.cb();`,
      [2, 5],
    ],
    [
      `globalThis.cb = () => alias.value();\nconst result = factory(callback);\nconst alias = {value: result};\nglobalThis.cb();`,
      [2, 5],
    ],
    [`globalThis.Klass = class { x = result(); };\nconst result = factory(callback);\nnew globalThis.Klass();`, [2, 4]],
  ];
  const [suffix, rawSide] = forms[i % forms.length];
  const side = rawSide.map((index) => index + shift);
  const bad = rename(base + suffix);
  const badSource = path.join(tmp, `unsafe-${i}.mjs`);
  writeFileSync(badSource, bad);
  const rejected = emit(`unsafe-${i}`, bad, side);
  assert.equal(rejected.result.status, 1, `${i}: ${rejected.result.stdout}\n${rejected.result.stderr}`);
  assert.match(rejected.result.stdout, /FAIL\s+OrderOk/, `unsafe ${i}`);
  assert.doesNotMatch(rejected.result.stdout, /inferred closure factory/, `unsafe ${i}`);
  assert(!existsSync(path.join(rejected.dir, "entry.mjs")), `unsafe ${i} emitted without --force`);
  const forced = emit(`forced-${i}`, bad, side, true);
  assert.equal(forced.result.status, 1, `forced ${i}`);
  assert.match(forced.result.stdout, /FAIL\s+OrderOk/, `forced ${i}`);
  const before = run(process.execPath, [badSource]);
  const after = run(process.execPath, [path.join(forced.dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.equal(before.stdout, `${value}\n`);
  assert.notEqual(after.status, 0, `forced ${i} unexpectedly matched`);
  assert.match(after.stderr, /ReferenceError/, `forced ${i}`);
  // Ensure the forced output is a real multi-module split, not a surrogate.
  assert.match(readFileSync(path.join(forced.dir, "entry.mjs"), "utf8"), /import/);
}
