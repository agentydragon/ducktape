import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { lower, prepare, root } from "./harness.mjs";

const provider = "nested/provider.mjs";
const lazy = "nested/lazy.mjs";
const entry = "entry.mjs";
const worker = "renderer.mjs";
const dir = path.join(root, "provider-reentry");
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
const { verdict } = lower(files.sourceFile, files.specFile, out, opts);
assert.equal(verdict.status, "rejected", JSON.stringify(verdict));
assert.equal(verdict.reason_code, "strict_observation_unsupported", JSON.stringify(verdict));
assert(!existsSync(out));

// The original graph still exposes distinct entry and provider module records.
// The strict CLI refuses to publish a moved version of this callable/import graph.
const api = await import(pathToFileURL(path.join(dir, entry)));
assert.deepEqual(Object.keys(api), ["entryMeta", "load", "meta", "render", "update"]);
assert.equal(api.meta().url, pathToFileURL(path.join(dir, provider)).href);
assert.equal(api.entryMeta().url, pathToFileURL(path.join(dir, entry)).href);
assert.notEqual(api.meta(), api.entryMeta());
api.meta().tag = "provider only";
assert.equal(api.entryMeta().tag, undefined);
const ns = await api.load();
assert.equal(ns.sample(), "before");
api.update("after");
assert.equal(ns.sample(), "after");
assert.equal(await api.load(), ns);

// Same provider file, different re-entry: the lazy module requires an export
// the real linked provider doesn't have. A root-relative decoy still exists.
writeFileSync(path.join(dir, lazy), 'import {missing} from "./provider.mjs"; export {missing};');
const refused = lower(files.sourceFile, files.specFile, path.join(dir, "unlinked"), opts, "linked-structural").verdict;
assert.equal(refused.status, "rejected");
assert.match(refused.reason, /does not export missing/);
assert(!existsSync(path.join(dir, "unlinked")));
const original = readFileSync(path.join(dir, provider), "utf8");
writeFileSync(path.join(dir, provider), original.replace('import("./lazy.mjs")', 'import("./provider.mjs")'));
assert.match(
  lower(files.sourceFile, files.specFile, path.join(dir, "direct"), opts, "linked-structural").verdict.reason,
  /directly requests a linked namespace/
);

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
const hidden = lower(
  capture.sourceFile,
  capture.specFile,
  path.join(captureDir, "unsafe"),
  opts,
  "linked-structural"
).verdict;
assert.equal(hidden.status, "rejected");
assert.match(hidden.reason, /linked state already exposes the region of unit 2/);

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
const nsCheck = (args, scope) => lower(nsFiles.sourceFile, nsFiles.specFile, nsOut, args, scope).verdict;
const strictRefusal = nsCheck(opts);
assert.equal(strictRefusal.status, "rejected", JSON.stringify(strictRefusal));
assert.equal(strictRefusal.reason_code, "strict_observation_unsupported", JSON.stringify(strictRefusal));
assert(!existsSync(nsOut));
