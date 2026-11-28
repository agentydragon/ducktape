import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parse } from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule } from "../../translation/scope_metadata.mjs";

const tmp = mkdtempSync(path.join(process.env.TEST_TMPDIR, "star-exports-"));
function stage(name, sources) {
  const dir = path.join(tmp, name);
  mkdirSync(dir);
  for (const [file, source] of Object.entries(sources)) writeFileSync(path.join(dir, file), source);
  return {
    asts: () =>
      Object.fromEntries(
        Object.keys(sources).map((file) => [
          file,
          dumpParsed(parseModule(readFileSync(path.join(dir, file), "utf8"), parse, eslintScope), {
            includeOpaqueCaptures: true,
          }),
        ])
      ),
    load: (file) => import(pathToFileURL(path.join(dir, file)).href),
  };
}
const entry = "app.mjs";
const sources = {
  "provider.mjs": "export let value = false; export const only = true; export default true;",
  "other.mjs": "export let value = false;",
  "left.mjs": 'export * from "./provider.mjs";',
  "right.mjs": 'import { value, only } from "./provider.mjs"; export { value, only };',
  "diamond.mjs": 'export * from "./left.mjs"; export * from "./right.mjs";',
  "conflict.mjs": 'export * from "./diamond.mjs"; export * from "./other.mjs";',
  "barrel.mjs": 'export * from "./conflict.mjs"; export { value as value } from "./diamond.mjs";',
  [entry]:
    'import {value, only} from "./barrel.mjs"; import {value as direct} from "./provider.mjs"; ' +
    'import {value as distinct} from "./other.mjs"; export {value, only, direct, distinct};',
};
// The producer itself executes the same write after linking both import paths.
sources["provider.mjs"] += " value = true;";
const original = stage("original", sources);
const reread = stage("reread", {
  ...sources,
  "diamond.mjs": '// A fresh parse with different offsets.\nexport * from "./left.mjs";\nexport * from "./right.mjs";',
});
const wrong = stage("wrong-provider", {
  ...sources,
  "barrel.mjs": sources["barrel.mjs"].replace('from "./diamond.mjs"', 'from "./other.mjs"'),
});
const badImport = stage("ambiguous-import", {
  ...sources,
  [entry]: 'import { value } from "./conflict.mjs"; export { value };',
});
const badDefault = stage("default-import", {
  ...sources,
  [entry]: 'import value from "./diamond.mjs"; export { value };',
});
const cycleSources = { "a.mjs": 'export * from "./b.mjs";', "b.mjs": 'export * from "./a.mjs";' };
const cycle = stage("cycle", cycleSources);
const queries = [
  [entry, "value"],
  [entry, "direct"],
  [entry, "only"],
  [entry, "distinct"],
  ["diamond.mjs", "value"],
];
const metadataQueries = [
  ["diamond.mjs", "default"],
  ["conflict.mjs", "value"],
  ["barrel.mjs", "value"],
];
const probe = (tree, kind = "starReadback", names = queries) => ({
  kind,
  input: {
    files: tree.asts(),
    entry,
    queries: names,
    ...(kind === "starReadback" ? { namespaceRead: ["barrel.mjs", "value"] } : {}),
  },
});
const [before, after, miswired, metadata, ambiguous, missing, cyclic] = JSON.parse(
  execFileSync(process.env.LINKED_INPUT_CHECKER, [], {
    input:
      JSON.stringify([
        probe(original),
        probe(reread),
        probe(wrong),
        probe(original, "starMetadata", metadataQueries),
        probe(badImport),
        probe(badDefault),
        probe(cycle, "starMetadata", []),
      ]) + "\n",
    encoding: "utf8",
  })
);

assert.ok(before.output, JSON.stringify(before));
assert.deepEqual(after, before);
assert.deepEqual(before.output.order, [
  "provider.mjs",
  "left.mjs",
  "right.mjs",
  "diamond.mjs",
  "other.mjs",
  "conflict.mjs",
  "barrel.mjs",
  entry,
]);
const [live, direct, only, distinct, diamond] = before.output.exports;
assert.deepEqual(live, direct);
assert.deepEqual(live, diamond);
assert.equal(live.value, true);
assert.equal(before.output.liveRead, true);
assert.equal(only.value, true);
assert.equal(distinct.value, false);
assert.notDeepEqual(live.cell, distinct.cell);
assert.ok(miswired.output, JSON.stringify(miswired));
assert.deepEqual(miswired.output.exports[0], distinct);
assert.equal(miswired.output.liveRead, false);
assert.deepEqual(metadata.output, [
  { error: "diamond.mjs: missing export default" },
  { error: "conflict.mjs: ambiguous export value" },
  { output: ["provider.mjs", "value"] },
]);
assert.match(ambiguous.error, /ambiguous export value/);
assert.match(missing.error, /missing export default/);
assert.match(cyclic.error, /cyclic static module records/);

for (const tree of [original, reread]) {
  const app = await tree.load(entry);
  assert.deepEqual([app.value, app.direct, app.only, app.distinct], [true, true, true, false]);
  assert.deepEqual(Object.keys(await tree.load("diamond.mjs")), ["only", "value"]);
  assert.deepEqual(Object.keys(await tree.load("conflict.mjs")), ["only"]);
}
assert.equal((await wrong.load(entry)).value, false);
await assert.rejects(badImport.load(entry), { name: "SyntaxError" });
await assert.rejects(badDefault.load(entry), { name: "SyntaxError" });
