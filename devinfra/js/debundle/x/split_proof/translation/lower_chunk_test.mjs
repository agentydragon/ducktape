// What a real chunk has and a hand-written bundle may not: `export` names that meet the glue's, `import`s from
// outside modules, and declarator lists. Each case splits a small bundle with `lowersplit` and compares what Node
// prints for the bundle and for the emitted entry: the output of evaluation, then the values of the public names.
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { acorn } from "./parser.mjs";
import { assertSourceUnitsReparse } from "./testing/ast_assertions.mjs";
import { nodeRecordingHostContract, split as certifySplit } from "./testing/split.mjs";

const temp = process.env.TEST_TMPDIR;
assert(temp, "TEST_TMPDIR is required");

const PROBE = `const ns = await import(process.argv[2]);
for (const name of process.argv.slice(3)) console.log(name, String(ns[name]));
`;

/** Node's view of a module: what evaluation prints, then the values of `names`. */
function observe(dir, file, names) {
  const target = path.isAbsolute(file) ? file : path.join(dir, file);
  const relative = path.relative(dir, target);
  const specifier = relative.startsWith(".") ? relative : `./${relative}`;
  const probe = spawnSync(process.execPath, [path.join(dir, "probe.mjs"), specifier, ...names], {
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
function split(name, { code, outside = {}, modules, spec = {} }) {
  const dir = path.join(temp, name);
  mkdirSync(dir, { recursive: true });
  writeFileSync(path.join(dir, "probe.mjs"), PROBE);
  writeFileSync(path.join(dir, "bundle.mjs"), code);
  for (const [file, text] of Object.entries(outside)) writeFileSync(path.join(dir, file), text);
  writeFileSync(path.join(dir, "spec.json"), JSON.stringify({ entry: "entry.mjs", modules, ...spec }));
  const placement = path.join(dir, "spec.json");
  const out = path.join(dir, "certified");
  const checked = certifySplit({
    source: path.join(dir, "bundle.mjs"),
    spec: placement,
    out,
    hostContract: nodeRecordingHostContract,
  });
  if (checked.status === "certified") {
    for (const file of Object.keys(outside)) copyFileSync(path.join(dir, file), path.join(out, file));
  }
  return {
    dir,
    out,
    checked,
    log: checked.log,
    text: (file) => readFileSync(path.join(dir, file), "utf8"),
  };
}

/** An accepted split prints what its bundle prints. */
function same(name, fixture, names) {
  const result = split(name, fixture);
  assert.equal(result.checked.status, "certified", `${name}: ${result.log}`);
  const original = observe(result.dir, "bundle.mjs", names);
  assert.doesNotMatch(original, /Error/, `${name}: the bundle itself fails`);
  assert.equal(observe(result.dir, path.join(result.out, "entry.mjs"), names), original, name);
  assertSourceUnitsReparse(fixture.code, result.out, Object.keys(fixture.modules), name);
  return result;
}

// A split that reverses visible effects must be refused without publishing files.
{
  const { checked, out, log } = split("inverted-effects", {
    code: 'console.log("first"); console.log("second");',
    modules: { "entry.mjs": ["#0"], "other.mjs": ["#1"] },
  });
  assert.equal(checked.status, "refused", log);
  assert(checked.reason, log);
  assert.equal(checked.files.length, 0);
  assert(!existsSync(path.join(out, "entry.mjs")));
}

// A glue name meets a name the entry publishes for another binding: `peel.mjs` needs the entry's local `m`, and the
// entry's own statement exports `X` as `m`. The linked checker currently cannot prove that the renamed export and
// the original entry export resolve to the same cell.
{
  const result = split("export_alias", {
    code: `const X = "public";
const m = "local";
function show() { return m; }
console.log(show());
export { X as m };
`,
    modules: { "entry.mjs": ["X", "m", "#3", "#4"], "peel.mjs": ["show"] },
  });
  assert.equal(result.checked.status, "refused", result.log);
  assert.equal(result.checked.reasonCode, "linked_input", result.log);
  assert.match(result.checked.reason, /entry exports resolve to different linked cells/i);
  assert.deepEqual(result.checked.files, []);
  assert(!existsSync(path.join(result.out, "entry.mjs")));
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
  const entryProgram = acorn.parse(readFileSync(path.join(result.out, "entry.mjs"), "utf8"), {
    sourceType: "module",
    ecmaVersion: "latest",
  });
  assert.equal(entryProgram.body.filter((node) => node.type === "ExportNamedDeclaration").length, 2);
  assert.deepEqual(importShape(path.join(result.out, "peel.mjs")), [
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
// statement, in the order of the requests. `peel.mjs` prints too and uses named and default imports.
const imports = {
  code: `import two, { one as uno } from "./dep.mjs";
import "./side.mjs";
console.log("first", uno);
function all() { return two() + uno; }
console.log("second", all());
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
// The linked input rules do not yet lower namespace imports. Count the
// refusal explicitly while retaining certified coverage for named, default,
// and side-effect-only imports.
{
  const refused = split("namespace_import", {
    code: `import * as ns from "./dep.mjs";\nfunction read() { return ns.three; }\nconsole.log(read());`,
    outside: imports.outside,
    modules: { "entry.mjs": ["#0", "#2"], "peel.mjs": ["#1"] },
  });
  assert.equal(refused.checked.status, "refused", refused.log);
  assert.equal(refused.checked.reasonCode, "linked_input", refused.log);
  assert.match(refused.checked.reason, /expected a named or default import/);
  assert.deepEqual(refused.checked.files, []);
  assert(!existsSync(path.join(refused.out, "entry.mjs")));
}
// Re-exports from another module are outside the linked input fragment too.
{
  const refused = split("external_reexport", {
    code: 'export { late } from "./late.mjs";',
    outside: imports.outside,
    modules: { "entry.mjs": ["#0"] },
  });
  assert.equal(refused.checked.status, "refused", refused.log);
  assert.equal(refused.checked.reasonCode, "linked_input", refused.log);
  assert.match(refused.checked.reason, /unsupported re-export from another module/);
  assert.deepEqual(refused.checked.files, []);
  assert(!existsSync(path.join(refused.out, "entry.mjs")));
}
{
  const result = same(
    "imports",
    { ...imports, modules: { "entry.mjs": ["#0", "#1", "#4"], "peel.mjs": ["#2", "all"] } },
    []
  );
  assert.deepEqual(importShape(path.join(result.out, "peel.mjs")), [
    {
      source: "./dep.mjs",
      specifiers: [
        { kind: "default", imported: null, local: "two" },
        { kind: "named", imported: "one", local: "uno" },
      ],
    },
  ]);
  // every request of the bundle comes before the glue import, in the bundle's order
  assert.deepEqual(importShape(path.join(result.out, "entry.mjs")), [
    {
      source: "./dep.mjs",
      specifiers: [
        { kind: "default", imported: null, local: "two" },
        { kind: "named", imported: "one", local: "uno" },
      ],
    },
    { source: "./side.mjs", specifiers: [] },
    { source: "./peel.mjs", specifiers: [{ kind: "named", imported: "all", local: "all" }] },
  ]);
  assert.deepEqual(exportShape(path.join(result.out, "entry.mjs")), []);
  const entryAst = acorn.parse(readFileSync(path.join(result.out, "entry.mjs"), "utf8"), {
    sourceType: "module",
    ecmaVersion: "latest",
  });
  assert.deepEqual(
    entryAst.body
      .filter((node) => node.type === "ExportNamedDeclaration" && node.source)
      .map((node) => node.source.value),
    []
  );
}

// An `import` moved out of the entry is refused.
{
  const fixture = { ...imports, modules: { "entry.mjs": ["#1", "#2", "#4"], "peel.mjs": ["#0", "#3", "all"] } };
  const refused = split("import_outside_entry", fixture);
  assert.equal(refused.checked.status, "refused", refused.log);
  assert.match(refused.checked.reason, /ImportDeclaration.*outside the entry module/);
  assert(!existsSync(path.join(refused.out, "entry.mjs")));
}

// `export * from` stays outside the model.
{
  const refused = split("export_all", {
    code: `export * from "./dep.mjs";\n`,
    outside: imports.outside,
    modules: { "entry.mjs": ["#0"] },
  });
  assert.equal(refused.checked.status, "refused", refused.log);
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
  assert.equal(unordered.checked.status, "refused", unordered.log);
  assert(unordered.checked.reason, unordered.log);
  const result = same("sequential", { ...fixture, spec: { sequential: true } }, []);
  assert.deepEqual(importShape(path.join(result.out, "c.mjs")), [
    { source: "./a.mjs", specifiers: [{ kind: "named", imported: "first", local: "first" }] },
    { source: "./b.mjs", specifiers: [] },
  ]);
  assert.deepEqual(importShape(path.join(result.out, "entry.mjs")), [{ source: "./c.mjs", specifiers: [] }]);
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
    assert.equal(refused.checked.status, "refused", `${refusedStatement}: ${refused.log}`);
  }
  for (const [name, taken] of [
    ["await_in_function", `const a = (async () => await Promise.resolve(1))();`],
    ["indirect_eval", `const a = (0, eval)("1");`],
  ]) {
    const refused = split(name, { code: `${taken}\n${rest}`, modules });
    assert.equal(refused.checked.status, "refused", refused.log);
    assert(refused.checked.reasonCode, refused.log);
    assert(refused.checked.reason, refused.log);
    assert.deepEqual(refused.checked.files, []);
    assert(!existsSync(path.join(refused.out, "entry.mjs")));
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
