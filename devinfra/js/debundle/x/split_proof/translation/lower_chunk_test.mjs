// What a real chunk has and a hand-written bundle may not: `export` names that meet the glue's, `import`s from
// outside modules, and declarator lists. Each case splits a small bundle with `lowersplit` and compares what Node
// prints for the bundle and for the emitted entry: the output of evaluation, then the values of the public names.
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { acorn, dump } from "./parser.mjs";
import { assertSourceUnitsReparse } from "./testing/ast_assertions.mjs";
import { writeDeployment } from "./testing/deployment_manifest.mjs";

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

function importShape(file) {
  const code = readFileSync(file, "utf8");
  const program = acorn.parse(code, { sourceType: "module", ecmaVersion: "latest" });
  return program.body
    .filter((node) => node.type === "ImportDeclaration")
    .map(({ source, specifiers }) => ({
      source: source.value,
      specifiers: specifiers.map((specifier) => ({
        kind:
          specifier.type === "ImportDefaultSpecifier"
            ? "default"
            : specifier.type === "ImportNamespaceSpecifier"
              ? "namespace"
              : "named",
        imported:
          specifier.type === "ImportSpecifier"
            ? specifier.imported.type === "Identifier"
              ? specifier.imported.name
              : specifier.imported.value
            : null,
        local: specifier.local.name,
      })),
    }));
}

function exportShape(file) {
  const code = readFileSync(file, "utf8");
  const program = acorn.parse(code, { sourceType: "module", ecmaVersion: "latest" });
  return program.body
    .filter((node) => node.type === "ExportNamedDeclaration" && node.declaration === null && node.source === null)
    .flatMap((node) => node.specifiers)
    .map(({ local, exported }) => ({ local: local.name, exported: exported.name }));
}

/** Dump `code`, split it by `modules` (the entry is `entry.mjs`) and write the result beside the bundle. */
function split(name, { code, outside = {}, modules, force = false, spec = {} }) {
  const dir = path.join(temp, name);
  mkdirSync(dir, { recursive: true });
  writeFileSync(path.join(dir, "probe.mjs"), PROBE);
  writeFileSync(path.join(dir, "bundle.mjs"), code);
  for (const [file, text] of Object.entries(outside)) writeFileSync(path.join(dir, file), text);
  const program = dump(code);
  writeFileSync(path.join(dir, "ast.json"), JSON.stringify(program));
  writeFileSync(path.join(dir, "spec.json"), JSON.stringify({ entry: "entry.mjs", modules, ...spec }));
  const checked = spawnSync(
    process.env.SPLIT_PREVIEW,
    [
      writeDeployment(path.join(dir, "ast.json"), path.join(dir, "spec.json"), dir),
      dir,
      "--plan",
      path.join(dir, "plan.json"),
      ...(force ? ["--force"] : []),
    ],
    { encoding: "utf8" }
  );
  const text = (file) => readFileSync(path.join(dir, file), "utf8");
  return { dir, checked, log: `${checked.stdout}\n${checked.stderr}`, text };
}

/** An accepted split prints what its bundle prints. */
function same(name, fixture, names) {
  const result = split(name, fixture);
  assert.equal(result.checked.status, 0, `${name}: ${result.log}`);
  const original = observe(result.dir, "bundle.mjs", names);
  assert.doesNotMatch(original, /Error/, `${name}: the bundle itself fails`);
  assert.equal(observe(result.dir, "entry.mjs", names), original, name);
  assertSourceUnitsReparse(fixture.code, result.dir, Object.keys(fixture.modules), name);
  return result;
}

// A forced split reverses two visible effects. Report the pair in source ordinals.
{
  const { checked, text, log } = split("inverted-effects", {
    code: 'console.log("first"); console.log("second");',
    modules: { "entry.mjs": ["#0"], "other.mjs": ["#1"] },
    force: true,
  });
  assert.equal(checked.status, 1, log);
  assert.deepEqual(JSON.parse(text("plan.json")).orderConflicts, {
    count: 1,
    examples: [{ earlier: 0, later: 1 }],
  });
}

// A glue name meets a name the entry publishes for another binding: `peel.mjs` needs the entry's local `m`, and the
// entry's own statement exports `X` as `m`. The glue exports the local under a new name.
{
  const result = same(
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
  assert.deepEqual(exportShape(path.join(result.dir, "entry.mjs")), [
    { local: "X", exported: "m" },
    { local: "m", exported: "m$1" },
  ]);
  assert.deepEqual(importShape(path.join(result.dir, "peel.mjs")), [
    { source: "./entry.mjs", specifiers: [{ kind: "named", imported: "m$1", local: "m" }] },
  ]);
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
  const result = same(
    "export_reuse",
    { code, modules: { "entry.mjs": ["base", "local", "#4", "#5"], "peel.mjs": ["twice", "sum"] } },
    ["base", "local"]
  );
  const entryProgram = acorn.parse(readFileSync(path.join(result.dir, "entry.mjs"), "utf8"), {
    sourceType: "module",
    ecmaVersion: "latest",
  });
  assert.equal(entryProgram.body.filter((node) => node.type === "ExportNamedDeclaration").length, 2);
  assert.deepEqual(importShape(path.join(result.dir, "peel.mjs")), [
    {
      source: "./entry.mjs",
      specifiers: [
        { kind: "named", imported: "base", local: "base" },
        { kind: "named", imported: "local", local: "local" },
      ],
    },
  ]);
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
  const result = same(
    "imports",
    { ...imports, modules: { "entry.mjs": ["#0", "#1", "#2", "#5", "#6"], "peel.mjs": ["#3", "all"] } },
    ["late"]
  );
  assert.deepEqual(importShape(path.join(result.dir, "peel.mjs")), [
    {
      source: "./dep.mjs",
      specifiers: [
        { kind: "default", imported: null, local: "two" },
        { kind: "named", imported: "one", local: "uno" },
      ],
    },
    {
      source: "./dep.mjs",
      specifiers: [{ kind: "namespace", imported: null, local: "ns" }],
    },
  ]);
  // every request of the bundle comes before the glue import, in the bundle's order
  assert.deepEqual(importShape(path.join(result.dir, "entry.mjs")), [
    {
      source: "./dep.mjs",
      specifiers: [
        { kind: "default", imported: null, local: "two" },
        { kind: "named", imported: "one", local: "uno" },
      ],
    },
    {
      source: "./dep.mjs",
      specifiers: [{ kind: "namespace", imported: null, local: "ns" }],
    },
    { source: "./side.mjs", specifiers: [] },
    { source: "./peel.mjs", specifiers: [{ kind: "named", imported: "all", local: "all" }] },
  ]);
  assert.deepEqual(exportShape(path.join(result.dir, "entry.mjs")), []);
  const entryAst = acorn.parse(readFileSync(path.join(result.dir, "entry.mjs"), "utf8"), {
    sourceType: "module",
    ecmaVersion: "latest",
  });
  assert.deepEqual(
    entryAst.body
      .filter((node) => node.type === "ExportNamedDeclaration" && node.source)
      .map((node) => node.source.value),
    ["./late.mjs"]
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

// Modules that have to run in the bundle's order, though an early one mentions a later one: `a.mjs` calls `helper`
// of `c.mjs`, so an import of `c.mjs` from `a.mjs` alone runs `c.mjs` first. With `sequential`, `c.mjs` is entered
// first and imports `a.mjs` and `b.mjs` before its own code runs.
{
  const fixture = {
    code: [
      `const first = () => helper();`,
      `console.log("a");`,
      `console.log("b");`,
      `function helper() { return 1; }`,
      `console.log("c", first());`,
      `console.log("end");`,
      ``,
    ].join("\n"),
    modules: { "a.mjs": ["#0", "#1"], "b.mjs": ["#2"], "c.mjs": ["#3", "#4"], "entry.mjs": ["#5"] },
  };
  const unordered = split("sequential_off", fixture);
  assert.equal(unordered.checked.status, 1, unordered.log);
  assert.match(unordered.log, /FAIL +OrderOk/);
  const result = same("sequential", { ...fixture, spec: { sequential: true } }, []);
  assert.deepEqual(importShape(path.join(result.dir, "c.mjs")), [
    { source: "./a.mjs", specifiers: [{ kind: "named", imported: "first", local: "first" }] },
    { source: "./b.mjs", specifiers: [] },
  ]);
  assert.deepEqual(importShape(path.join(result.dir, "entry.mjs")), [{ source: "./c.mjs", specifiers: [] }]);
  // the same order, given by name
  same("sequential_order", { ...fixture, spec: { sequential: true, order: ["a.mjs", "b.mjs", "c.mjs"] } }, []);
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
  same("declarators", { code, modules: { "entry.mjs": ["b", "e", "f", "#5"], "peel.mjs": ["a", "c"] } }, ["e", "f"]);
}
