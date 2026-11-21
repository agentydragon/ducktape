// What a real chunk has and a hand-written bundle may not: `export` names that meet the glue's, `import`s from
// outside modules, and declarator lists. Each case splits a small bundle with `lowersplit` and compares what Node
// prints for the bundle and for the emitted entry: the output of evaluation, then the values of the public names.
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

// A glue name meets a name the entry publishes for another binding: `peel.mjs` needs the entry's local `m`, and the
// entry's own statement exports `X` as `m`. The glue exports the local under a new name.
{
  const { text } = same(
    "export_alias",
    {
      code: `const X = "public";
const m = "local";
function show() { return m; }
console.log(show());
export { X as m };
`,
      modules: { "entry.mjs": ["X", "m", "#3", "#4"], "peel.mjs": ["show"] },
    },
    ["m"]
  );
  assert.match(text("entry.mjs"), /^export \{ X as m \};$/m);
  assert.match(text("entry.mjs"), /^export \{ m as m\$1 \};$/m);
  assert.match(text("peel.mjs"), /^import \{ m\$1 as m \} from "\.\/entry\.mjs";$/m);
}

// The entry already publishes the bindings `peel.mjs` needs under their own names: no second export.
{
  const code = `export const base = 2;
function twice() { return base * 2; }
const local = 3;
function sum() { return base + local; }
console.log(twice(), sum());
export { local };
`;
  const { text } = same(
    "export_reuse",
    { code, modules: { "entry.mjs": ["base", "local", "#4", "#5"], "peel.mjs": ["twice", "sum"] } },
    ["base", "local"]
  );
  assert.equal(text("entry.mjs").match(/^export /gm).length, 2);
  assert.match(text("peel.mjs"), /^import \{ base, local \} from "\.\/entry\.mjs";$/m);
}

// Imports from outside modules. Each outside module prints when it is evaluated: in the bundle that is before any
// statement, in the order of the requests. `peel.mjs` prints too and uses a named, a default and a namespace import.
const imports = {
  code: `import two, { one as uno } from "./dep.mjs";
import * as ns from "./dep.mjs";
import "./side.mjs";
console.log("first", uno);
function all() { return [two(), ns.three]; }
console.log("second", all());
export { late } from "./late.mjs";
`,
  outside: {
    "dep.mjs": `console.log("dep");
export const one = 1;
export default function two() { return 2; }
export const three = 3;
`,
    "side.mjs": `console.log("side");\n`,
    "late.mjs": `console.log("late");\nexport const late = 4;\n`,
  },
};
{
  const { text } = same(
    "imports",
    { ...imports, modules: { "entry.mjs": ["#0", "#1", "#2", "#5", "#6"], "peel.mjs": ["#3", "all"] } },
    ["late"]
  );
  assert.deepEqual(text("peel.mjs").split("\n").slice(0, 3), [
    'import two from "./dep.mjs";',
    'import { one as uno } from "./dep.mjs";',
    'import * as ns from "./dep.mjs";',
  ]);
  // every request of the bundle comes before the glue import, in the bundle's order
  assert.deepEqual(
    text("entry.mjs")
      .split("\n")
      .filter((line) => / from |^import /.test(line)),
    [
      'import two, { one as uno } from "./dep.mjs";',
      'import * as ns from "./dep.mjs";',
      'import "./side.mjs";',
      'export { late } from "./late.mjs";',
      'import { all } from "./peel.mjs";',
    ]
  );
}

// An `import` moved out of the entry is refused: written anyway, `side.mjs` runs after `late.mjs`.
{
  const fixture = { ...imports, modules: { "entry.mjs": ["#0", "#1", "#5", "#6"], "peel.mjs": ["#2", "#3", "all"] } };
  const refused = split("import_outside_entry", fixture);
  assert.equal(refused.checked.status, 1, refused.log);
  assert.match(refused.checked.stdout, /FAIL\s+statement 2 is an `ImportDeclaration` outside the entry module/);
  assert(!existsSync(path.join(refused.dir, "entry.mjs")));
  const forced = split("import_outside_entry_forced", { ...fixture, force: true });
  assert.equal(forced.checked.status, 1, forced.log);
  assert.notEqual(observe(forced.dir, "entry.mjs", ["late"]), observe(forced.dir, "bundle.mjs", ["late"]));
}

// `export * from` stays outside the model.
{
  const refused = split("export_all", {
    code: `export * from "./dep.mjs";\n`,
    outside: imports.outside,
    modules: { "entry.mjs": ["#0"] },
  });
  assert.equal(refused.checked.status, 3, refused.log);
}

// Outside the model, refused before any check: a top-level `await` (the module that awaits lets a module imported
// after it run first), a top-level `using`, and a direct `eval` anywhere (it reaches bindings by their spelling, so a
// split can part it from a name no statement mentions). The same split is taken with the `await` inside an async
// function and with an indirect eval, which runs in the global scope.
{
  const modules = { "first.mjs": ["#0", "#1"], "second.mjs": ["#2"], "entry.mjs": ["#3"] };
  const rest = `console.log("first", typeof a);\nconsole.log("second");\nconsole.log("third");\n`;
  for (const refusedStatement of [
    `const a = await Promise.resolve(1);`,
    `for await (const x of [1]) globalThis.a = x;`,
    `using a = { [Symbol.dispose]() {} };`,
    `const a = eval("1");`,
    `const a = () => eval("rest");`,
  ]) {
    const refused = split("outside_model", { code: `${refusedStatement}\n${rest}`, modules });
    assert.equal(refused.checked.status, 3, `${refusedStatement}: ${refused.log}`);
  }
  for (const [name, taken] of [
    ["await_in_function", `const a = (async () => await Promise.resolve(1))();`],
    ["indirect_eval", `const a = (0, eval)("1");`],
  ]) {
    same(name, { code: `${taken}\n${rest}`, modules }, []);
  }
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
