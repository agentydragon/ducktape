// What a real chunk has and a hand-written bundle may not: declarator lists. Each case splits a small bundle with
// `lowersplit` and compares what Node prints for the bundle and for the emitted entry: the output of evaluation, then
// the values of the public names.
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const temp = process.env.TEST_TMPDIR;
assert(temp, "TEST_TMPDIR is required");

const PROBE = `const ns = await import(process.argv[2]);
for (const name of process.argv.slice(3)) console.log(name, String(ns[name]));
`;

/** Node's view of a module: what evaluation prints, then the values of `names`. */
function observe(dir, file, names) {
  const probe = spawnSync(process.execPath, [path.join(dir, "probe.mjs"), `./${file}`, ...names], {
    cwd: dir,
    encoding: "utf8",
  });
  return probe.status === 0 ? probe.stdout : `${probe.stdout}${probe.stderr.match(/^\w*Error: .*/m)?.[0]}`;
}

/** Dump `code`, split it by `modules` (the entry is `entry.mjs`) and write the result beside the bundle. */
function split(name, { code, outside = {}, modules, force = false }) {
  const dir = path.join(temp, name);
  mkdirSync(dir, { recursive: true });
  writeFileSync(path.join(dir, "probe.mjs"), PROBE);
  writeFileSync(path.join(dir, "bundle.mjs"), code);
  for (const [file, text] of Object.entries(outside)) writeFileSync(path.join(dir, file), text);
  const dump = lowerDump(code, acorn, eslintScope);
  writeFileSync(path.join(dir, "ast.json"), JSON.stringify(dump));
  writeFileSync(path.join(dir, "spec.json"), JSON.stringify({ entry: "entry.mjs", modules }));
  const checked = spawnSync(
    process.env.LOWERSPLIT,
    [path.join(dir, "ast.json"), path.join(dir, "spec.json"), dir, ...(force ? ["--force"] : [])],
    { encoding: "utf8" }
  );
  const text = (file) => readFileSync(path.join(dir, file), "utf8");
  return { dir, dump, checked, log: `${checked.stdout}\n${checked.stderr}`, text };
}

/** An accepted split prints what its bundle prints. */
function same(name, fixture, names) {
  const result = split(name, fixture);
  assert.equal(result.checked.status, 0, `${name}: ${result.log}`);
  const original = observe(result.dir, "bundle.mjs", names);
  assert.doesNotMatch(original, /Error/, `${name}: the bundle itself fails`);
  assert.equal(observe(result.dir, "entry.mjs", names), original, name);
  return result;
}

// One statement per declarator, also under `export`: the declarators of a list go to different modules. A
// destructuring declarator stays whole.
{
  const code = `const a = 1, b = a + 1, { c, d } = { c: 3, d: 4 };
export let e = 5, f = () => e + b;
console.log(a, b, c, d, e, f());
`;
  const { dump, text } = same(
    "declarators",
    { code, modules: { "entry.mjs": ["b", "e", "f", "#5"], "peel.mjs": ["a", "c"] } },
    ["e", "f"]
  );
  assert.deepEqual(
    dump.statements.map((s) => s.src),
    [
      "const a = 1;",
      "const b = a + 1;",
      "const { c, d } = { c: 3, d: 4 };",
      "export let e = 5;",
      "export let f = () => e + b;",
      "console.log(a, b, c, d, e, f());",
    ]
  );
  assert.equal(text("peel.mjs"), "const a = 1;\nconst { c, d } = { c: 3, d: 4 };\n\nexport { a, c, d };\n");
}
