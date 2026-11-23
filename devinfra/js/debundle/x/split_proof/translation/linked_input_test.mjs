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

function check({
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
  const result = spawnSync(process.env.LINKED_INPUT_CHECKER, [], {
    encoding: "utf8",
    input: JSON.stringify({ source, providers: { "providers/maker.mjs": parse(maker) }, files, glueBinding }) + "\n",
  });
  if (result.error) throw result.error;
  return result;
}

const accepted = check();
assert.equal(accepted.status, 0, accepted.stderr);
assert.deepEqual(JSON.parse(accepted.stdout), [
  [5, 1, 2, 3],
  [5, 2, 3, 1],
]);

for (const [controls, reason] of [
  [{ glueBinding: "alternate" }, /unit 1: reference to extra resolves to a different linked cell/],
  [{ request: "./providers/maker.mjs" }, /entry requests for providers differ/],
  [{ entryExports: "export { deferred as changed };" }, /named exports differ/],
  [
    { maker: "var make = function (cb) { return () => arguments; }; export { make as build };" },
    /unsupported implicit arguments reference/,
  ],
]) {
  const rejected = check(controls);
  assert.equal(rejected.status, 1, rejected.stderr + rejected.stdout);
  assert.match(rejected.stderr, reason);
}

// A public alias through a glue import must resolve to the same declaration.
const reexport = check({ exposesGlue: true });
assert.equal(reexport.status, 0, reexport.stderr);

const redirected = check({ exposesGlue: true, glueBinding: "alternate" });
assert.equal(redirected.status, 1, redirected.stderr + redirected.stdout);
assert.match(redirected.stderr, /entry exports resolve to different linked cells/);

function opaqueUnits(ast) {
  const result = spawnSync(process.env.LINKED_INPUT_CHECKER, ["opaque-units"], {
    encoding: "utf8",
    input: JSON.stringify(ast) + "\n",
  });
  if (result.error) throw result.error;
  return result;
}

const opaque = opaqueUnits(
  parse(`
  import { seed } from "./provider.mjs";
  const config = { seed: 7 };
  function make(local, width = config, ...tail) {
    return () => [seed(local), width, config, tail, make, window];
  }
  if (seed) { var ready = config; let hidden = ready; }
  class Store { method(config) { return [config, seed]; } }
`)
);
assert.equal(opaque.status, 0, opaque.stderr);
assert.deepEqual(JSON.parse(opaque.stdout), [
  [1, [["config", "const"]], [], [], "world"],
  [2, [], [["make", 1, true]], ["config", "seed"], "skip"],
  [3, [["ready", "var"]], [], ["config", "seed"], "world"],
  [4, [["Store", "let"]], [], ["seed"], "world"],
]);

for (const [source, reason] of [
  ["await ready;", /top-level `await`/],
  ['function read() { return eval("value"); }', /direct `eval`/],
  ['const later = () => import("./later.mjs");', /source-module or method context/],
  ["const base = import.meta.url;", /source-module or method context/],
  ["function* steps() { yield 3; }", /async or generator/],
]) {
  const rejected = opaqueUnits(parse(source));
  assert.equal(rejected.status, 1, rejected.stderr + rejected.stdout);
  assert.match(rejected.stderr, reason);
}
