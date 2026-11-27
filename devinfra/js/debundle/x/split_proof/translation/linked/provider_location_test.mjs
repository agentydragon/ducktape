import assert from "node:assert/strict";
import { existsSync, mkdirSync } from "node:fs";
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
const out = path.join(dir, "out");
const { verdict, log } = lower(files.sourceFile, files.specFile, out);
assert.equal(verdict.reason_code, "no_constructed_rule", log);
assert(!existsSync(out));
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
