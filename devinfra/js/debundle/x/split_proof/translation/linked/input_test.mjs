import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dump } from "../parser.mjs";

const parse = (source) => dump(source);
const provider = `
var make = (cb) => { const invoke = () => cb(); return invoke; };
export { make as build };
`;
const call = "var deferred = wrap(function callback() { return extra(); });";
const closures = "var extra = () => 17; var alternate = () => 42;";
const publish = "export { deferred as start };";
const publishBoth = "export { deferred as start, extra as readExtra };";

function linked({
  maker = provider,
  request = "../providers/maker.mjs",
  glueBinding = "extra",
  exposesGlue = false,
  entryExports = exposesGlue ? publishBoth : publish,
} = {}) {
  const sourceExports = exposesGlue ? publishBoth : publish;
  const source = parse(`import { build as wrap } from "./providers/maker.mjs"; ${call} ${closures} ${sourceExports}`);
  const files = {
    "entry/main.mjs": parse(`
      import { build as wrap } from "${request}";
      import { extra } from "../lib/closures.mjs";
      ${call}
      ${entryExports}
    `),
    "lib/closures.mjs": parse(`${closures}\nexport { ${glueBinding} as extra };`),
  };
  return { source, providers: { "providers/maker.mjs": parse(maker) }, files, glueBinding };
}

const orders = [
  [5, 1, 2, 3],
  [5, 2, 3, 1],
];
const cases = [{ kind: "linked", input: linked(), expected: orders }];
const inlineLexicalSource = "let seed = 7; let ready = seed + 2; export let result = ready;\n";
const inlineLexicalSpec = {
  entry: "entry.mjs",
  modules: { "entry.mjs": ["#1", "#2"], "worker.mjs": ["#0"] },
  order: ["worker.mjs"],
  sequential: true,
};
cases.push({
  kind: "indexedInlinePlan",
  input: {
    source: parse(inlineLexicalSource),
    sourceText: inlineLexicalSource,
    spec: inlineLexicalSpec,
  },
  expected: [
    [0, 1, 2],
    [0, 1, 2],
    [
      [1, "entry.mjs"],
      [2, "entry.mjs"],
      [0, "worker.mjs"],
    ],
    true,
    ["result"],
  ],
});
cases.push({
  kind: "indexedInlinePlan",
  input: {
    source: parse(inlineLexicalSource),
    sourceText: inlineLexicalSource,
    spec: { ...inlineLexicalSpec, modules: { ...inlineLexicalSpec.modules, "entry.mjs": ["#1", "#2", "#1"] } },
  },
  expected: /each executable row must be selected exactly once/,
});

// Sample the source-size-independent proof on real parsed const and let chains.
// The last binding keeps its exact address, and both graphs agree at zero fuel
// and on their complete final state (checked by the Lean runner).
cases.push({
  kind: "inlineConstructor",
  input: {
    source: parse(""),
    sourceText: "",
    spec: { entry: "entry.mjs", modules: { "entry.mjs": [] }, order: [], sequential: true },
  },
  expected: { entry: "entry.mjs", order: ["entry.mjs"], placements: [] },
});
for (const kind of ["const", "let"]) {
  for (const count of [1, 2, 5, 19, 64]) {
    const ids = Array.from({ length: count }, (_, i) => i);
    const cut = Math.floor(count / 2);
    const sourceText = ids
      .map((i) => `${i === count - 1 ? "export " : ""}${kind} binding${i} = ${i === 0 ? "7" : `binding${i - 1}`};`)
      .join("\n");
    const selector = (i) => (count === 2 ? `binding${i}` : `#${i}`);
    const cells = [[`binding${count - 1}`, count, 0]];
    cases.push({
      kind: "inlineConstructor",
      input: {
        source: parse(sourceText),
        sourceText,
        spec: {
          ...inlineLexicalSpec,
          modules: { "entry.mjs": ids.slice(cut).map(selector), "worker.mjs": ids.slice(0, cut).map(selector) },
        },
        checkFuel: 16,
      },
      expected: {
        entry: "entry.mjs",
        order: ["worker.mjs", "entry.mjs"],
        placements: [
          ...ids.slice(cut).map((i) => [i, "entry.mjs"]),
          ...ids.slice(0, cut).map((i) => [i, "worker.mjs"]),
        ],
        sourceCells: cells,
        outputCells: cells,
      },
    });
  }
}
cases.push({
  kind: "singletonPlacement",
  input: linked(),
  expected: [
    [
      [1, "entry/main.mjs"],
      [2, "lib/closures.mjs"],
      [3, "lib/closures.mjs"],
      [5, "providers/maker.mjs"],
    ],
    "decoded output units differ from the planned placement",
  ],
});
for (const [controls, reason] of [
  [{ maker: "export const wrong = 0;" }, /original linker: the module providers\/maker\.mjs does not export build/],
  [{ glueBinding: "alternate" }, /unit 1: reference to extra resolves to a different linked cell/],
  [{ request: "./providers/maker.mjs" }, /entry requests for providers differ/],
  [{ entryExports: "export { deferred as changed };" }, /named exports differ/],
  [{ maker: "var make = function (cb) { return () => arguments; }; export { make as build };" }, orders],
]) {
  cases.push({ kind: "linked", input: linked(controls), expected: reason });
}

const entryFile = "dist/main.js";
const targetFile = "assets/lazy.js";
const resource = (file, source) => ({ url: new URL(file, "https://example.org/app/").href, source });
function locations({
  expr = '() => [import.meta.url, import("../assets/lazy.js")]',
  emptyMap = false,
  originalName = entryFile,
  emittedName = entryFile,
  moved = [1],
  retained = [0],
  before = {},
  after = {},
} = {}) {
  const later = `const later = ${expr};`;
  const scroll = "const scroll = () => 42;";
  const text = `${later} ${scroll}`;
  const old = { [entryFile]: resource(entryFile, text), [targetFile]: resource(targetFile, "export const x = 1;") };
  return {
    source: parse(text),
    originalName,
    emptyMap,
    emittedName,
    retained,
    moved,
    before: { ...old, ...before },
    after: {
      ...old,
      [entryFile]: resource(entryFile, later),
      "new/scroll.mjs": resource("new/scroll.mjs", scroll),
      ...after,
    },
  };
}

cases.push({
  kind: "location",
  input: locations(),
  expected: [
    [0, "import.meta.url"],
    [0, "../assets/lazy.js", targetFile],
  ],
});
const bare = '() => import("@fixture/diagram")';
cases.push({
  kind: "location",
  input: locations({ expr: bare, emptyMap: true }),
  expected: [[0, "rejected unmapped import", "@fixture/diagram"]],
});
for (const [opts, expected] of [
  [{ expr: bare }, /expected a relative module request/],
  [{ expr: bare, emptyMap: true, moved: [0], retained: [1] }, /URL-sensitive unit 0 moved/],
  [{ expr: '() => import("https://example.org/lib.js")', emptyMap: true }, /expected a relative module request/],
  [{ expr: '() => import("/lib.js")', emptyMap: true }, /expected a relative module request/],
  [{ expr: "() => import(name)", emptyMap: true }, /expected a string literal/],
  [{ expr: '() => import("./missing.js")', emptyMap: true }, /missing original dynamic target/],
  [{ originalName: "alias.js" }, /source entry path differs/],
  [{ before: { [entryFile]: resource(entryFile, "different") } }, /parsed source differs/],
  [
    { after: { [entryFile]: { ...resource(entryFile, ""), url: "https://other.example/dist/main.js" } } },
    /effective entry URL changed/,
  ],
  [{ moved: [0], retained: [1] }, /URL-sensitive unit 0 moved/],
  [{ before: { "new/scroll.mjs": resource("new/scroll.mjs", "") } }, /output path already exists/],
  [{ after: { [targetFile]: resource(targetFile, "export const x = 2;") } }, /target URL or source changed/],
  [{ after: { [targetFile]: resource("elsewhere.js", "export const x = 1;") } }, /target URL or source changed/],
  [{ expr: '() => import("../assets/missing.js")' }, /missing original dynamic target/],
  [{ expr: '() => import("./main.js")' }, /dynamic target is an output module/],
  [{ expr: '() => import("../../outside.js")' }, /leaves the module tree/],
  [{ expr: "() => import(name)" }, /expected a string literal/],
  [{ expr: '() => import("./dep.js", {with: {type: "json"}})' }, /unsupported dynamic import options/],
  [{ expr: "() => import.meta" }, /expected import.meta.url/],
  [{ expr: "() => import.meta.resolve" }, /expected import.meta.url/],
  [{ expr: '() => eval("scroll")' }, /direct `eval`/],
  [{ expr: 'await import("./lazy.js")' }, /top-level `await`/],
]) {
  cases.push({ kind: "location", input: locations(opts), expected });
}

// A lazy module imports the editor through a helper. Its internal static cycle must terminate.
function dynamicGraph({ target = "assets/lazy.js", helper, omitHelper = false, after = {} } = {}) {
  const helperName = "assets/helper.js";
  const source = {
    [target]: 'import "./helper.js"; import "../runtime.js"; export default import.meta.url;',
    [helperName]: helper ?? 'import "./lazy.js"; import { api as read } from "../editor.js"; export { read };',
  };
  const resources = Object.fromEntries(Object.entries(source).map(([path, source]) => [path, resource(path, source)]));
  const parsed = Object.fromEntries(Object.entries(source).map(([path, source]) => [path, parse(source)]));
  if (omitHelper) delete parsed[helperName];
  return { target, linked: ["editor.js", "runtime.js"], parsed, before: resources, after: { ...resources, ...after } };
}
cases.push({
  kind: "imports",
  input: dynamicGraph(),
  expected: [
    ["assets/lazy.js", "assets/helper.js"],
    [
      ["assets/helper.js", "editor.js", ["api"]],
      ["assets/lazy.js", "runtime.js", []],
    ],
  ],
});
for (const [opts, expected] of [
  [{ target: "editor.js" }, /directly requests a linked namespace/],
  [{ omitHelper: true }, /missing parsed dynamic dependency assets\/helper.js/],
  [
    { after: { "assets/helper.js": resource("assets/helper.js", "export const changed = true;") } },
    /URL or source changed/,
  ],
  [{ helper: 'import * as editor from "../editor.js";' }, /expected a named or default import/],
  [{ helper: 'export { api } from "../editor.js";' }, /unsupported re-export/],
  [{ helper: 'const next = () => import("../editor.js");' }, /nested dynamic import/],
  [{ helper: "await start();" }, /top-level `await`/],
]) {
  cases.push({ kind: "imports", input: dynamicGraph(opts), expected });
}

cases.push({
  kind: "opaque",
  input: parse("const load = 0; async function open(...args) { return (await load).default(...args); }"),
  expected: [
    [0, [["load", "const"]], [], [], "world"],
    [1, [], [["open", 0, "async"]], ["load"], "skip"],
  ],
});
// A public alias through a glue import must resolve to the same declaration.
cases.push({ kind: "linked", input: linked({ exposesGlue: true }), expected: orders });
cases.push({
  kind: "linked",
  input: linked({ exposesGlue: true, glueBinding: "alternate" }),
  expected: /extra 0; missing 0; retargeted 1: "readExtra" \(original cell @3\.0 -> candidate cell @4\.0\)/,
});

cases.push({
  kind: "opaque",
  input: parse(`
  import { seed } from "./provider.mjs";
  const config = { seed: 7 };
  function make(local, width = config, ...tail) {
    return () => [seed(local), width, config, tail, make, window];
  }
  if (seed) { var ready = config; let hidden = ready; }
  class Store { method(config) { return [config, seed]; } }
`),
  expected: [
    [1, [["config", "const"]], [], [], "world"],
    [2, [], [["make", 1, true]], ["config", "seed"], "skip"],
    [3, [["ready", "var"]], [], ["config", "seed"], "world"],
    [4, [["Store", "let"]], [], ["seed"], "world"],
  ],
});

cases.push({
  kind: "constructorContexts",
  input: parse("class C extends Error { constructor() { super(); } }"),
  expected: [true, false, true],
});
cases.push({
  kind: "opaque",
  input: parse(`const Parent = Error;
    class ExportError extends Parent { constructor(message) { super(message); this.name = "ExportError"; } }
    const Wrapped = class extends Parent { constructor(...args) { super(...args); } };`),
  expected: [
    [0, [["Parent", "const"]], [], [], "world"],
    [1, [["ExportError", "let"]], [], ["Parent"], "world"],
    [2, [["Wrapped", "const"]], [], ["Parent"], "world"],
  ],
});
for (const [source, reason] of [
  [
    "class C extends Error { constructor() { const init = () => super(); init(); } }",
    /source-module or method context/,
  ],
  ["class C extends Error { constructor() { super(new.target); } }", /source-module or method context/],
  ['class C extends Error { constructor() { super(eval("a")); } }', /direct `eval`/],
  ["await ready;", /top-level `await`/],
  ['function read() { return eval("value"); }', /direct `eval`/],
  ['const later = () => import("./later.mjs");', /source-module or method context/],
  ["const base = import.meta.url;", /source-module or method context/],
  ["function* steps() { yield 3; }", /unsupported opaque generator/],
]) {
  cases.push({ kind: "opaque", input: parse(source), expected: reason });
}

// The same module-scoped binding moves into an import in the last statement.
// The first initializer remains an opaque typed-array construction and its reader hoists.
function structural({ glueBinding = "scroll", pivot = 3, init = "new Uint8Array([seed()])", emittedInit = init } = {}) {
  const request = 'import { seed } from "./provider.mjs";';
  const read = "function first() { return bytes[0]; }";
  const arrow = "var scroll = ({ offset }) => offset + 1;";
  const suffix = "var run = () => scroll({ offset: first() }); export { run };";
  return {
    pivot,
    glueBinding,
    source: parse(`${request} const bytes = ${init}; ${read} ${arrow} ${suffix}`),
    providers: { "provider.mjs": parse("export const seed = () => 9;") },
    files: {
      "entry.mjs": parse(
        `${request} import { scroll } from "./lib/scroll.mjs"; const bytes = ${emittedInit}; ${read} ${suffix}`
      ),
      "lib/scroll.mjs": parse(`import { seed } from "../provider.mjs"; ${arrow} export { ${glueBinding} as scroll };`),
    },
  };
}
const seed = [6, ["seed"], []];
const allocation = [1, ["bytes"], ["seed"]];
const first = [2, ["first"], ["bytes"]];
const pivot = [3, "scroll"];
const run = [4, ["run"], ["first", "scroll"]];
cases.push({
  kind: "structural",
  input: structural(),
  expected: [
    [seed, allocation, first, pivot, run],
    [seed, pivot, allocation, first, run],
  ],
});
cases.push({
  kind: "structuralSparseHole",
  input: parse("const sparse = [1, , (() => 2)()];"),
  expected: {
    reason: "statement 0: unsupported array hole",
    reason_code: "structural_unsupported_sparse_array_hole",
  },
});
cases.push({
  kind: "structuralSparseHole",
  input: parse("const sparse = [1, , 2];"),
  expected: { accepted: true },
});
for (const [controls, expected] of [
  [{ glueBinding: "seed" }, /unit 4: reference to scroll resolves to a different linked cell/],
  [{ emittedInit: "new Float32Array([seed()])" }, /emitted syntax differs from original unit 1/],
  [{ init: "import.meta.url" }, /requires source-module or method context/],
  [{ init: "class extends Uint8Array { read() { return super.value; } }" }, /requires source-module or method context/],
  [{ pivot: 0 }, /pivot 0 is not an executable source unit/],
]) {
  cases.push({ kind: "structural", input: structural(controls), expected });
}

// Lean elaborates the test entry once for the whole fixture table.
const result = spawnSync(process.env.LINKED_INPUT_CHECKER, [], {
  encoding: "utf8",
  input: JSON.stringify(cases.map(({ kind, input }) => ({ kind, input }))) + "\n",
});
if (result.error) throw result.error;
assert.equal(result.status, 0, result.stderr);
const outputs = JSON.parse(result.stdout);
assert.equal(outputs.length, cases.length);
for (const [i, { expected }] of cases.entries()) {
  if (expected instanceof RegExp) assert.match(outputs[i].error, expected);
  else assert.deepEqual(outputs[i], { output: expected }, `fixture ${i}: ${JSON.stringify(outputs[i])}`);
}
