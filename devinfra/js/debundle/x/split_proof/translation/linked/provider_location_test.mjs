import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { prepare, root } from "./harness.mjs";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";

const provider = "nested/provider.mjs";
const lazy = "nested/lazy.mjs";
const entry = "entry.mjs";
const worker = "renderer.mjs";
const dir = path.join(root, "provider-reentry");

function invokePublic(files, out, args = []) {
  const manifest = writeDeployment(files.sourceFile, files.specFile, out);
  const result = spawnSync(process.env.LOWERSPLIT, [manifest, out, ...args], { encoding: "utf8" });
  assert.ifError(result.error);
  assert([0, 1].includes(result.status), result.stdout + result.stderr);
  const verdict = JSON.parse(result.stdout.trim());
  return { result, verdict };
}

function assertRefused(files, out, args = [], reasonCode = null) {
  const { result, verdict } = invokePublic(files, out, args);
  assert.equal(result.status, 1, result.stdout + result.stderr);
  assert.equal(verdict.status, "rejected", result.stdout + result.stderr);
  assert.equal(verdict.guarantee, null, result.stdout + result.stderr);
  if (reasonCode !== null) assert.equal(verdict.reason_code, reasonCode, result.stdout + result.stderr);
  assert(!existsSync(out), `refusal created ${out}`);
  return verdict;
}

function writePreview(files, out) {
  const manifest = writeDeployment(files.sourceFile, files.specFile, out);
  const result = spawnSync(process.env.SPLIT_PREVIEW, [manifest, out, "--force"], { encoding: "utf8" });
  assert.ifError(result.error);
  assert([0, 1].includes(result.status), result.stdout + result.stderr);
  assert.match(result.stdout, /model diagnostic: /, result.stdout + result.stderr);
  assert(existsSync(out), `preview was not written to ${out}`);
  return result.stdout;
}

function snapshotFiles(directory) {
  return Object.fromEntries(
    readdirSync(directory)
      .sort()
      .map((name) => [name, readFileSync(path.join(directory, name))])
  );
}

mkdirSync(path.join(dir, "nested"), { recursive: true });
const files = prepare(dir, {
  sourceName: entry,
  source: `import { load, meta, update } from "./${provider}";
          var first = 0; var render = () => 42; var entryMeta = () => import.meta;
          export { load, meta, update, render, entryMeta };`,
  provider: "",
  spec: { entry, modules: { [entry]: ["#0", "#1", "#3", "#4"], [worker]: ["#2"] }, order: [worker], sequential: true },
  extras: {
    [provider]: `export var current = "before";
                export var update = value => {current = value;};
                export var meta = () => import.meta;
                export var load = () => import("./lazy.mjs");`,
    [lazy]: `import {current} from "./provider.mjs"; import "./cycle.mjs"; export const sample = () => current;`,
    "nested/cycle.mjs": 'import "./lazy.mjs";',
    "lazy.mjs": 'export const sample = () => "wrong referrer";',
  },
});
const host = path.join(dir, "host.json");
writeFileSync(host, JSON.stringify({ profile: "browser-page" }));
const opts = [
  "--structural",
  "--deployment-origin",
  "https://example.org",
  "--host-contract",
  host,
  "--deployment-preset",
  "native-browser-record@1",
];
const out = path.join(dir, "out");
const deployment = writeDeployment(files.sourceFile, files.specFile, path.join(dir, "public-refusal"));
assertPublicEmissionRefused(deployment, path.join(dir, "public-refusal"));
assertRefused(files, path.join(dir, "former-mode-refusal"), opts);
writePreview(files, out);

const previewBytes = snapshotFiles(out);
const previewManifest = writeDeployment(files.sourceFile, files.specFile, out);
assertPublicRecheckRefused(previewManifest, out);
const checkedWithFormerMode = invokePublic(files, out, [...opts, "--check-output"]);
assert.equal(
  checkedWithFormerMode.result.status,
  1,
  checkedWithFormerMode.result.stdout + checkedWithFormerMode.result.stderr
);
assert.equal(
  checkedWithFormerMode.verdict.status,
  "rejected",
  checkedWithFormerMode.result.stdout + checkedWithFormerMode.result.stderr
);
assert.deepEqual(snapshotFiles(out), previewBytes, "former-mode check-output changed the preview bytes");

// An overlay serves lazy assets and providers at their original URLs. Recreate
// it in a second tree to exercise both module graphs without sharing Node's cache.
const overlay = path.join(root, "overlay");
cpSync(dir, overlay, { recursive: true, filter: (file) => file !== out });
cpSync(out, overlay, { recursive: true });
for (const tree of [dir, overlay]) {
  const api = await import(pathToFileURL(path.join(tree, entry)));
  assert.deepEqual(Object.keys(api), ["entryMeta", "load", "meta", "render", "update"]);
  assert.equal(api.meta().url, pathToFileURL(path.join(tree, provider)).href);
  assert.equal(api.entryMeta().url, pathToFileURL(path.join(tree, entry)).href);
  assert.notEqual(api.meta(), api.entryMeta());
  api.meta().tag = "provider only";
  assert.equal(api.entryMeta().tag, undefined);
  const ns = await api.load();
  assert.equal(ns.sample(), "before");
  api.update("after");
  assert.equal(ns.sample(), "after");
  assert.equal(await api.load(), ns);
}

// Same provider file, different re-entry: the lazy module requires an export
// the real linked provider doesn't have. A root-relative decoy still exists.
writeFileSync(path.join(dir, lazy), 'import {missing} from "./provider.mjs"; export {missing};');
const refused = assertRefused(files, path.join(dir, "unlinked"));
assert.match(refused.reason, /does not export missing/);
const original = readFileSync(path.join(dir, provider), "utf8");
writeFileSync(path.join(dir, provider), original.replace('import("./lazy.mjs")', 'import("./provider.mjs")'));
const direct = assertRefused(files, path.join(dir, "direct"));
assert.match(direct.reason, /directly requests a linked namespace/);

// The retained provider's hoist can obtain the entry's live pivot cell only
// through the external lazy script. That capability is already in the ready
// heap. Merely allowing import() syntax on a copied file would miss it.
const captureDir = path.join(root, "provider-capture");
const capture = prepare(captureDir, {
  sourceName: entry,
  source: `import {load} from "./provider.mjs"; var start = load(); var pivot = 7; export {pivot,start};`,
  provider: 'export function load(){return import("./lazy.mjs");}',
  extras: { "lazy.mjs": 'import {pivot} from "./entry.mjs"; export const sample = () => pivot;' },
  spec: { entry, modules: { [entry]: ["#0", "#1", "#3"], [worker]: ["#2"] }, order: [worker], sequential: true },
});
const hiddenOut = path.join(captureDir, "unsafe");
const hiddenManifest = writeDeployment(capture.sourceFile, capture.specFile, hiddenOut);
assertPublicEmissionRefused(hiddenManifest, hiddenOut);

// Both providers export the same names. Retargeting a relocated namespace
// import must still fail disk verification, even with byte-identical providers.
const nsDir = path.join(root, "static-namespace");
const moduleText = "export let value = 1; export function read() { return this.value; }";
const nsFiles = prepare(nsDir, {
  sourceName: entry,
  source:
    'import * as ns from "./provider.mjs"; import "./other.mjs"; function sample(){return ns.read();} export {sample};',
  provider: moduleText,
  extras: { "other.mjs": moduleText },
  spec: { entry, modules: { [entry]: ["#0", "#1", "#3"], [worker]: ["#2"] }, order: [worker], sequential: true },
});
const nsOut = path.join(nsDir, "out");
writePreview(nsFiles, nsOut);
const relocated = path.join(nsOut, worker);
const emitted = readFileSync(relocated, "utf8");
assert.match(emitted, /provider\.mjs/);
writeFileSync(relocated, emitted.replace("provider.mjs", "other.mjs"));
const tamperedManifest = writeDeployment(nsFiles.sourceFile, nsFiles.specFile, nsOut);
assertPublicRecheckRefused(tamperedManifest, nsOut);
