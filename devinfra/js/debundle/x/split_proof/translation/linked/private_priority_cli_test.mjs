import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { split } from "../testing/split.mjs";

const root = process.env.TEST_TMPDIR;
const source = path.join(root, "source.mjs");
const spec = path.join(root, "spec.json");
const entry = "entry.mjs";
const residual = "residual.mjs";
const original = {
  entry,
  modules: { [entry]: ["#1"], [residual]: ["#0"] },
  order: [residual],
  privateEntryBody: true,
};
const run = (placement, out, checkOutput = false) => {
  writeFileSync(spec, JSON.stringify(placement));
  return split({ source, spec, out, checkOutput, require: "normal-completion" });
};

function assertCertified(result) {
  assert.equal(result.status, "certified", result.log);
  assert.equal(result.guarantee, "all-outcomes", result.log);
  assert.equal(result.scope, "indexed-local-exports", result.log);
  assert.equal(result.verdict.strategy, "compiled-constructor", result.log);
}

function assertRefused(placement, label) {
  const out = path.join(root, label);
  const result = run(placement, out);
  assert.equal(result.status, "refused", result.log);
  assert(!existsSync(out));
}

// These are the corpus adapter's original translated placement and source
// bytes for emission/catchall_exports/unclaimed. No sequential override.
writeFileSync(source, readFileSync(process.env.CATCHALL_SOURCE));
const out = path.join(root, "catchall");
const result = run(original, out);
assertCertified(result);
assert.deepEqual(result.files, [entry, residual]);
const entryFile = path.join(out, entry);
const provider = path.join(out, residual);
assert.match(readFileSync(provider, "utf8"), /\bconst a = 1;/);
const namespace = await import(pathToFileURL(entryFile));
assert.deepEqual(Object.entries(namespace), [
  ["foo", 1],
  ["x-y", 1],
]);
const probe = spawnSync(
  process.execPath,
  [
    "--input-type=module",
    "-e",
    "const {default: read} = await import(process.argv[1]); read(await import(process.argv[2]));",
    pathToFileURL(process.env.CATCHALL_PROBE).href,
    pathToFileURL(entryFile).href,
  ],
  { encoding: "utf8" }
);
assert.equal(probe.status, 0, probe.stderr);
assert.equal(probe.stdout, "1 1\n");
assertCertified(run(original, out, true));

// Keep the binding and both original public values but leak an extra name.
const changed = readFileSync(entryFile, "utf8") + "\nexport { a };\n";
writeFileSync(entryFile, changed);
assert.equal(run(original, out, true).status, "refused");
assert.equal(readFileSync(entryFile, "utf8"), changed);
assertRefused({ ...original, order: [] }, "omitted-priority");
assertRefused({ ...original, order: [residual, residual] }, "duplicate-priority");
assertRefused({ ...original, modules: { [entry]: ["#1"], [residual]: ["#0", "#0"] } }, "duplicate-ordinal");
assertRefused({ ...original, modules: { [entry]: [], [residual]: ["#0", "#1"] } }, "export-row-moved");

// Generic order control: no dependency forces the two owners into source order.
// Reversing a complete priority must not be silently repaired.
writeFileSync(source, "const a = 1; const b = 2; export { a, b };\n");
const twoOwners = {
  ...original,
  modules: { [entry]: ["#2"], "a.mjs": ["#0"], "b.mjs": ["#1"] },
  order: ["a.mjs", "b.mjs"],
};
assertCertified(run(twoOwners, path.join(root, "two-owners")));
assertRefused({ ...twoOwners, order: ["b.mjs", "a.mjs"] }, "reversed-priority");

// Even the same observable values in the namespace cannot replace live cells.
const cellsOut = path.join(root, "cell-aliases");
writeFileSync(source, 'const a = 1; const b = 1; export { a as foo, b as "x-y" };\n');
assertCertified(run(twoOwners, cellsOut));
const cellsEntry = path.join(cellsOut, entry);
const cellBytes = readFileSync(cellsEntry, "utf8");
const wrongCell = cellBytes.replace(/\bb as (["'])x-y\1/, 'a as "x-y"');
assert.notEqual(wrongCell, cellBytes);
writeFileSync(cellsEntry, wrongCell);
assert.equal(run(twoOwners, cellsOut, true).status, "refused");
assert.equal(readFileSync(cellsEntry, "utf8"), wrongCell);
