import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const parse = (source) => lowerDump(source, acorn, eslintScope);
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
for (const [controls, reason] of [
  [{ glueBinding: "alternate" }, /unit 1: reference to extra resolves to a different linked cell/],
  [{ request: "./providers/maker.mjs" }, /entry requests for providers differ/],
  [{ entryExports: "export { deferred as changed };" }, /named exports differ/],
  [
    { maker: "var make = function (cb) { return () => arguments; }; export { make as build };" },
    /unsupported implicit arguments reference/,
  ],
]) {
  cases.push({ kind: "linked", input: linked(controls), expected: reason });
}

// A public alias through a glue import must resolve to the same declaration.
cases.push({ kind: "linked", input: linked({ exposesGlue: true }), expected: orders });
cases.push({
  kind: "linked",
  input: linked({ exposesGlue: true, glueBinding: "alternate" }),
  expected: /entry exports resolve to different linked cells/,
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

for (const [source, reason] of [
  ["await ready;", /top-level `await`/],
  ['function read() { return eval("value"); }', /direct `eval`/],
  ['const later = () => import("./later.mjs");', /source-module or method context/],
  ["const base = import.meta.url;", /source-module or method context/],
  ["function* steps() { yield 3; }", /async or generator/],
]) {
  cases.push({ kind: "opaque", input: parse(source), expected: reason });
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
  else assert.deepEqual(outputs[i], { output: expected });
}
