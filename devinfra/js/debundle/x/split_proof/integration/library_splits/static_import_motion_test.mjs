import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parse } from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule } from "../../translation/scope_metadata.mjs";

const entry = "entry.mjs";
const facade = "locale.mjs";
const worker = "map.mjs";
const left = '// 🧪 UTF-16 source offsets\nimport { left as label } from "./left.mjs";\n';
const right = 'import { readOrder } from "./right.mjs";\n';
const definition = "const format = () => label + readOrder();\n";
const snapshot = "export default format;\n";
const spec = {
  entry: facade,
  modules: { [worker]: ["#0", "#1", "#2"], [facade]: ["#3"] },
  order: [worker],
  sequential: true,
};
const source = {
  "state.mjs": 'export let trace = ""; export function add(x) { trace += x; }',
  "left.mjs":
    'import {add} from "./state.mjs"; add("L"); export let left = "first:"; export const alternate = "wrong:"; export function set(x) { left = x; }',
  "right.mjs": 'import {add, trace} from "./state.mjs"; add("R"); export function readOrder() { return trace; }',
  [facade]: left + right + definition + snapshot,
  [entry]:
    'import format from "./locale.mjs"; export {format}; export {default} from "./locale.mjs"; export {left, set} from "./left.mjs";',
};
const output = {
  ...source,
  [facade]: 'import { format } from "./map.mjs";\n' + snapshot,
  [worker]: left + right + "export " + definition,
};

function stage(name, sources) {
  const dir = path.join(process.env.TEST_TMPDIR, name);
  mkdirSync(dir);
  for (const [file, text] of Object.entries(sources)) writeFileSync(path.join(dir, file), text);
  const files = Object.fromEntries(
    Object.keys(sources).map((file) => {
      const text = readFileSync(path.join(dir, file), "utf8");
      return [file, dumpParsed(parseModule(text, parse, eslintScope), { includeOpaqueCaptures: true })];
    })
  );
  return { files, load: () => import(pathToFileURL(path.join(dir, entry)).href) };
}

const original = stage("original", source);
const positive = stage("reread", output);
const wrongOrder = stage("request-order", { ...output, [worker]: right + left + "export " + definition });
const wrongCell = stage("wrong-cell", {
  ...output,
  [worker]: output[worker].replace("left as label", "alternate as label"),
});
const wrongEdge = stage("wrong-edge", {
  ...output,
  [worker]: output[worker].replace("./left.mjs", "./right.mjs").replace("left as label", "readOrder as label"),
});
const cycle = stage("cycle", { ...output, [worker]: output[worker].replace("./left.mjs", "./entry.mjs") });
const changedBody = stage("body", { ...output, [worker]: output[worker].replace("label +", 'label + "!" +') });
const wrongSnapshot = stage("snapshot", {
  ...output,
  [facade]: output[facade].replace(snapshot, "export { format as default };\n"),
});
const providerEdit = stage("provider", { ...output, "left.mjs": source["left.mjs"].replace('add("L")', 'add("X")') });

const asyncSource = stage("async-original", { ...source, "left.mjs": source["left.mjs"] + " await 0;" });
const asyncCandidate = stage("async-candidate", { ...output, "left.mjs": output["left.mjs"] + " await 0;" });

const requests = [positive, wrongOrder, wrongCell, wrongEdge, cycle, changedBody, wrongSnapshot, providerEdit].map(
  ({ files }) => ({
    kind: "importMotion",
    input: { entry, spec, before: original.files, reread: files },
  })
);
requests.push({
  kind: "importMotion",
  input: { entry, spec, before: asyncSource.files, reread: asyncCandidate.files },
});
const [pass, order, alias, edge, cyclic, body, snapshotAlias, fixed, asynchronous] = JSON.parse(
  execFileSync(process.env.LINKED_INPUT_CHECKER, [], { encoding: "utf8", input: JSON.stringify(requests) + "\n" })
);

assert.ok(pass.output, JSON.stringify(pass));
assert.deepEqual(pass.output, {
  requests: 2,
  before: ["state.mjs", "left.mjs", "right.mjs", facade, entry],
  after: ["state.mjs", "left.mjs", "right.mjs", worker, facade, entry],
  concrete: { output: "all modeled runs agree" },
});
assert.match(order.error, /evaluation order/);
assert.match(alias.error, /named bindings/);
assert.match(edge.error, /evaluation order|named bindings/);
assert.match(cyclic.error, /cyclic static module records/);
assert.match(body.error, /original rows and exact glue/);
assert.match(snapshotAlias.error, /original rows and exact glue/);
assert.match(fixed.error, /fixed provider bytes changed/);
assert.match(asynchronous.error, /asynchronous module evaluation is unsupported/);

async function observe(tree) {
  const api = await tree.load();
  assert.deepEqual(Object.keys(api), ["default", "format", "left", "set"]);
  assert.equal(api.default, api.format);
  const before = [api.default(), api.left];
  api.set("changed:");
  return [before, api.default(), api.left];
}
assert.deepEqual(await observe(original), [["first:LR", "first:"], "changed:LR", "changed:"]);
assert.deepEqual(await observe(positive), [["first:LR", "first:"], "changed:LR", "changed:"]);
assert.deepEqual(await observe(wrongOrder), [["first:RL", "first:"], "changed:RL", "changed:"]);
assert.deepEqual(await observe(wrongCell), [["wrong:LR", "first:"], "wrong:LR", "changed:"]);
