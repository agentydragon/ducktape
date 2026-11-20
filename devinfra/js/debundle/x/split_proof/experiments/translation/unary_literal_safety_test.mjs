import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const temp = process.env.TEST_TMPDIR;
assert(temp);
const here = path.dirname(fileURLToPath(import.meta.url));
const run = (bin, args) => spawnSync(bin, args, { encoding: "utf8" });
const driver = path.join(temp, "driver.mjs");
writeFileSync(
  driver,
  `import { pathToFileURL } from "node:url";
if (process.argv[3] === "regexp") RegExp.prototype[Symbol.toPrimitive] = () => globalThis.saved();
try { await import(pathToFileURL(process.argv[2]).href); console.log("ok"); }
catch (e) { try { console.log(e.name + ":" + globalThis.saved()); } catch (inner) { console.log(e.name + ":" + inner.name); } }
`
);
function prepare(name, code, side) {
  const dir = path.join(temp, name);
  mkdirSync(dir, { recursive: true });
  const source = path.join(temp, `${name}.mjs`);
  const input = path.join(temp, `${name}-ast.json`);
  const spec = path.join(temp, `${name}-spec.json`);
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
  return { dir, source, input, spec };
}

for (const [name, expression, mode] of [
  ["bigint_plus", "+1n", "bigint"],
  ["regexp_plus", "+/x/", "regexp"],
  ["regexp_minus", "-/x/", "regexp"],
  ["regexp_bitnot", "~/x/", "regexp"],
]) {
  const code = `globalThis.saved = () => watched;\nconst watched = 1;\nconst value = ${expression};`;
  const { dir, source, input, spec } = prepare(name, code, [0, 2]);
  const original = run(process.execPath, [driver, source, mode]);
  assert.equal(original.status, 0, original.stderr);
  const checked = run(process.env.LOWERSPLIT, [input, spec, dir]);
  console.log(name, "unforced", checked.status, checked.stdout);
  if (checked.status === 1) assert(!existsSync(path.join(dir, "entry.mjs")), name);
  // Keep a force-emitted comparison even when the checker correctly refuses.
  const forced = run(process.env.LOWERSPLIT, [input, spec, dir, "--force"]);
  const after = run(process.execPath, [driver, path.join(dir, "entry.mjs"), mode]);
  console.log(name, "original", original.stdout.trim(), "emitted", after.stdout.trim(), "forced", forced.status);
  assert.equal(after.status, 0, after.stderr);
  assert.equal(original.stdout, mode === "bigint" ? "TypeError:1\n" : "ok\n", name);
  assert.equal(
    after.stdout,
    mode === "bigint" ? "TypeError:ReferenceError\n" : "ReferenceError:ReferenceError\n",
    name
  );
  assert.equal(checked.status, 1, `${name}: ${checked.stdout}\n${checked.stderr}`);
  assert.match(checked.stdout, /FAIL\s+OrderOk/, name);
  assert.equal(forced.status, 1, name);
  const frontend = path.join(here, "frontend.mjs"),
    bundle = path.join(temp, `${name}-bundle.json`),
    tree = path.join(temp, `${name}-tree.json`),
    cert = path.join(temp, `${name}-cert.json`);
  for (const args of [
    [frontend, "bundle", source, "--out", bundle],
    [frontend, "tree", dir, "--entry", "entry.mjs", "--out", tree],
    [path.join(here, "pair.mjs"), bundle, tree, "--out", cert],
  ]) {
    const r = run(process.execPath, args);
    assert.equal(r.status, 0, r.stderr);
  }
  const translated = run(process.env.TRANSLATE, [bundle, tree, cert]);
  assert.equal(translated.status, 4, `${name}: ${translated.stdout}\n${translated.stderr}`);
  assert.match(translated.stdout, /FAIL\s+OrderOk/, name);
}

for (const [name, expression] of [
  ["number_plus", "+1"],
  ["string_minus", '-"1"'],
  ["bigint_minus", "-1n"],
  ["bigint_bitnot", "~1n"],
  ["regexp_not", "!/x/"],
  ["number_bitnot", "~1"],
]) {
  const code = `const watched = 7;\nconst value = ${expression};\nfunction get() { return watched; }\nconsole.log(get(),String(value));`;
  const { dir, source, input, spec } = prepare(`safe_${name}`, code, [1]);
  const checked = run(process.env.LOWERSPLIT, [input, spec, dir]);
  assert.equal(checked.status, 0, `${name}: ${checked.stdout}\n${checked.stderr}`);
  assert(existsSync(path.join(dir, "entry.mjs")), name);
  const before = run(process.execPath, [source]),
    after = run(process.execPath, [path.join(dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.equal(after.status, 0, after.stderr);
  assert.equal(after.stdout, before.stdout, name);
}
