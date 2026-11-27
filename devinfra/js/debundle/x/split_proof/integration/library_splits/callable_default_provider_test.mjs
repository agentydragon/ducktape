import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, cpSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

const resolve = createRequire(import.meta.url).resolve;
const sourceBytes = new Map();
for (const [name, hash] of [
  ["_baseSortedUniq.js", "0cb97ac908e675c379134659f8814fd244017ef0c401913c3f6bb6282feb69ef"],
  ["eq.js", "3a51729bde3af1d9bacf9b67e966d8c154e22b02f5e1cecc13f53f7f00889c13"],
  ["sortedUniq.js", "0b23aa21cf887a1db228d20ecc388f5cc4ad6a0f40a3e8935d4aa44410a69c95"],
]) {
  const bytes = readFileSync(resolve(`lodash-es/${name}`));
  assert.equal(createHash("sha256").update(bytes).digest("hex"), hash, name);
  sourceBytes.set(name, bytes);
}
const manifest = resolve("lodash-es/package.json");
const { name, version, type } = JSON.parse(readFileSync(manifest, "utf8"));
assert.deepEqual({ name, version, type }, { name: "lodash-es", version: "4.18.1", type: "module" });

const tmp = process.env.TEST_TMPDIR;
const entry = "_baseSortedUniq.js";
const helper = "sorted-uniq.mjs";
// Match the authored placement in #20644; its baseline fixture and constructor dispatch are separate.
const request = {
  spec: { entry, order: [helper], modules: { [entry]: ["#0", "#2"], [helper]: ["#1"] }, sequential: true },
  entryFile: resolve(`lodash-es/${entry}`),
  providerFile: resolve("lodash-es/eq.js"),
  work: path.join(tmp, "work"),
  out: path.join(tmp, "split"),
  checkOnly: false,
};
const run = (args) =>
  spawnSync(process.env.CALLABLE_PROVIDER_CHECKER, {
    input: JSON.stringify(args) + "\n",
    encoding: "utf8",
  });
const written = run(request);
assert.equal(written.status, 0, written.stderr);
const reread = run({ ...request, checkOnly: true });
assert.equal(reread.status, 0, reread.stderr);
const model = JSON.parse(written.stdout);
assert.deepEqual(model, {
  providerComparisons: [true, true, true, false],
  positiveZero: true,
  nan: true,
  originalObjectIdentities: true,
});
assert.deepEqual(JSON.parse(reread.stdout), model);
assert.deepEqual(readdirSync(request.out).sort(), [entry, "eq.js", helper].sort());
assert.deepEqual(readFileSync(path.join(request.out, "eq.js")), sourceBytes.get("eq.js"));

const readModule = (file) => import(pathToFileURL(file).href);
const external = path.join(tmp, "external");
mkdirSync(external);
copyFileSync(manifest, path.join(external, "package.json"));
writeFileSync(path.join(external, "sortedUniq.js"), sourceBytes.get("sortedUniq.js"));
for (const file of readdirSync(request.out)) copyFileSync(path.join(request.out, file), path.join(external, file));
const [{ default: original }, { default: emitted }, { default: callee }, moved] = await Promise.all([
  readModule(resolve("lodash-es/sortedUniq.js")),
  readModule(path.join(external, "sortedUniq.js")),
  readModule(path.join(external, entry)),
  readModule(path.join(external, helper)),
]);
assert.equal(moved.baseSortedUniq, callee);
for (const sortedUniq of [original, emitted]) {
  const a = { value: 1 };
  const b = { value: 1 };
  const input = [-0, 0, NaN, NaN, a, a, b, b];
  const actual = sortedUniq(input);
  assert.deepEqual(actual, [0, NaN, a, b]);
  assert(Object.is(actual[0], +0));
  assert(Number.isNaN(actual[1]));
  assert.equal(actual[2], a);
  assert.equal(actual[3], b);
  assert.notEqual(actual, input);
  assert.deepEqual(input, [-0, 0, NaN, NaN, a, a, b, b]);
  assert.notEqual(sortedUniq([]), sortedUniq([]));
}

const changed = path.join(tmp, "modified-provider");
cpSync(request.out, changed, { recursive: true });
writeFileSync(path.join(changed, "eq.js"), Buffer.concat([sourceBytes.get("eq.js"), Buffer.from("\n")]));
const refused = run({ ...request, out: changed, checkOnly: true });
assert.equal(refused.status, 1, refused.stderr);
assert.match(refused.stderr, /eq\.js/);
const liveAlias = path.join(tmp, "live-alias");
cpSync(request.out, liveAlias, { recursive: true });
const aliased = path.join(liveAlias, entry);
const originalEntry = readFileSync(aliased, "utf8");
const replacement = originalEntry.replace("export default baseSortedUniq;", "export { baseSortedUniq as default };");
assert.notEqual(replacement, originalEntry);
writeFileSync(aliased, replacement);
const wrongCell = run({ ...request, out: liveAlias, checkOnly: true });
assert.equal(wrongCell.status, 1, wrongCell.stderr);
assert.match(wrongCell.stderr, /_baseSortedUniq\.js differs from its generated ESTree/);

const wrongOwner = run({
  ...request,
  spec: { ...request.spec, modules: { [entry]: ["#0"], [helper]: ["#1", "#2"] } },
  checkOnly: true,
});
assert.equal(wrongOwner.status, 1, wrongOwner.stderr);
assert.match(wrongOwner.stderr, /only the original function declaration may move/);
console.log(JSON.stringify({ formatPremise: "original type=module package bytes", model }));
