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
const source = parse(`import { build as wrap } from "./providers/maker.mjs"; ${call} ${closures} ${publish}`);

function check({
  maker = provider,
  request = "../providers/maker.mjs",
  glueBinding = "extra",
  entryExports = publish,
} = {}) {
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
