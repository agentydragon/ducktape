import assert from "node:assert/strict";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { forcePreview, lower, prepare, root } from "./harness.mjs";

const entry = "entry.mjs";
const worker = "moved/worker.mjs";
const deployment = ["--deployment-origin", "https://split.example.org"];

const source = `
  import { load } from "./nested/provider.mjs";
  let value = 7;
  const set = (next) => value = next;
  const lazy = () => import("./outer.mjs");
  const independent = 19;
  export { value, set, lazy, load, independent };
`;
const assets = {
  "outer.mjs": 'import {read} from "./inner/read.mjs"; export {read};',
  "inner/read.mjs": 'import {value} from "../entry.mjs"; export const read = () => value;',
  "nested/provider.mjs": 'export const load = async () => await import("./leaf.mjs");',
  "nested/leaf.mjs": 'import {value} from "../entry.mjs"; export const read = () => 2 * value;',
  "moved/outer.mjs": "export const read = () => -1;",
};

function writeAssets(directory) {
  for (const [name, content] of Object.entries(assets)) {
    const dest = path.join(directory, name);
    mkdirSync(path.dirname(dest), { recursive: true });
    writeFileSync(dest, content);
  }
}

function make(name, moved, retained, text = source) {
  const dir = path.join(root, name);
  writeAssets(dir);
  const files = prepare(dir, {
    sourceName: entry,
    source: text,
    provider: "",
    spec: { entry, modules: { [worker]: [moved], [entry]: retained }, order: [worker], sequential: true },
  });
  return { ...files, dir, out: path.join(dir, "out") };
}

async function checkLive(file) {
  const api = await import(pathToFileURL(file));
  assert.equal(api.independent, 19);
  const originalTarget = await api.lazy();
  const providerTarget = await api.load();
  assert.equal(originalTarget.read(), 7);
  assert.equal(providerTarget.read(), 14);
  api.set(13);
  assert.equal(api.value, 13);
  assert.equal(originalTarget.read(), 13);
  assert.equal(providerTarget.read(), 26);
  assert.equal(await api.lazy(), originalTarget);
  assert.equal(await api.load(), providerTarget);
}

const retained = make("retained-importers", "#4", ["#0", "#1", "#2", "#3", "#5"]);
const noDeployment = lower(retained.sourceFile, retained.specFile, retained.out).verdict;
assert.match(noDeployment.reason, /dynamic import requires a checked deployment/);
assert(!existsSync(retained.out));
const verified = lower(retained.sourceFile, retained.specFile, retained.out, deployment).verdict;
assert.equal(verified.status, "certified", JSON.stringify(verified));
assert(verified.assumptions.some((premise) => premise.includes("checked live cells")));
assert.deepEqual(
  lower(retained.sourceFile, retained.specFile, retained.out, [...deployment, "--check-output"]).verdict,
  verified
);

writeFileSync(
  path.join(retained.dir, "nested/leaf.mjs"),
  'import {missing} from "../entry.mjs"; export const read = () => missing;'
);
const missing = lower(retained.sourceFile, retained.specFile, retained.out, [...deployment, "--check-output"]).verdict;
assert.equal(missing.status, "rejected", JSON.stringify(missing));
assert.match(missing.reason, /missing.*export|does not export/);

writeFileSync(path.join(retained.dir, "nested/leaf.mjs"), assets["nested/leaf.mjs"]);
// The certificate models an overlay at the original paths. The runtime probe
// copies the unchanged side files beside the emitted tree to run both layouts.
writeAssets(retained.out);
await checkLive(retained.sourceFile);
await checkLive(path.join(retained.out, entry));

const moved = make("different-referrer", "#3", ["#0", "#1", "#2", "#4", "#5"]);
const wrongReferrer = lower(moved.sourceFile, moved.specFile, moved.out, deployment).verdict;
assert.equal(wrongReferrer.status, "rejected", JSON.stringify(wrongReferrer));
assert.match(wrongReferrer.reason, /URL-sensitive unit 3 moved from the entry/);
assert(!existsSync(moved.out));
const preview = path.join(moved.dir, "preview");
forcePreview(moved, preview);
writeAssets(preview);
const original = await import(pathToFileURL(moved.sourceFile));
const split = await import(pathToFileURL(path.join(preview, entry)));
assert.equal((await original.lazy()).read(), 7);
assert.equal((await split.lazy()).read(), -1);

for (const [name, replacement, reason] of [
  ["eager-request", 'import("./outer.mjs")', /dynamic import.*outer.*is reached/],
  ["nonliteral-request", "() => import(value)", /expected a string literal/],
]) {
  const test = make(
    name,
    "#4",
    ["#0", "#1", "#2", "#3", "#5"],
    source.replace('() => import("./outer.mjs")', replacement)
  );
  const refusal = lower(test.sourceFile, test.specFile, test.out, deployment).verdict;
  assert.equal(refusal.status, "rejected", JSON.stringify(refusal));
  assert.match(refusal.reason, reason);
  assert(!existsSync(test.out));
}
