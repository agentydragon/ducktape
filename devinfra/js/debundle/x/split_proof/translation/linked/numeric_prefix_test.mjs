import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { forcePreview, lower, observe, prepare, root } from "./harness.mjs";

function fixture(name, source, moved, kept) {
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source,
    provider: "",
    spec: { entry: "entry.mjs", modules: { "numbers.mjs": moved, "entry.mjs": kept } },
  });
  const out = path.join(dir, "out");
  return {
    files,
    out,
    run: (scope, ...flags) => lower(files.sourceFile, files.specFile, out, ["--structural", ...flags], scope).verdict,
  };
}

const chain = fixture(
  "chain",
  "var seed = 2; var marker = 42; var double = 2 * seed; var epsilon = 1e-5; var limit = 2 * double - epsilon; export { seed, marker, limit };",
  ["#0", "#2", "#3", "#4"],
  ["#1", "#5"]
);
const emitted = chain.run("linked-structural-moves");
assert.equal(emitted.status, "certified", JSON.stringify(emitted));
assert.deepEqual(chain.run("linked-structural-moves", "--check-output"), emitted);
for (const file of [chain.files.sourceFile, path.join(chain.out, "entry.mjs")]) {
  assert.deepEqual(observe(file, "limit"), { limit: 8 - 1e-5 });
}

for (const [name, source, moved, kept, before, after] of [
  [
    "mutating-call",
    "let seed=2; const set=()=>{ seed=4; }; const alias=set; alias(); var result=2*seed; export {seed,set,result};",
    ["#0", "#1", "#4"],
    ["#2", "#3", "#5"],
    { result: 8 },
    { result: 4 },
  ],
  [
    "coercion",
    "Object.prototype.valueOf = () => 7; var seed = {}; var result=2*seed; export {result};",
    ["#1", "#2"],
    ["#0", "#3"],
    { result: 14 },
    { result: null },
  ],
  [
    "tdz",
    "const seed=2; var result=2*seed; export {seed,result};",
    ["#1"],
    ["#0", "#2"],
    { result: 4 },
    { error: "ReferenceError" },
  ],
]) {
  const test = fixture(name, source, moved, kept);
  assert.equal(test.run("linked-structural").status, "rejected");
  assert(!existsSync(test.out));
  const preview = path.join(root, name, "preview");
  forcePreview(test.files, preview);
  assert.deepEqual(observe(test.files.sourceFile, "result"), before);
  assert.deepEqual(observe(path.join(preview, "entry.mjs"), "result"), after);
}

const special = fixture(
  "binary64",
  "var marker=42; var zero=0/(0-1); var inverse=1/zero; var invalid=0/0; var sum=invalid+1; export {zero,inverse,sum};",
  ["#1", "#2", "#3", "#4"],
  ["#0", "#5"]
);
assert.equal(special.run("linked-structural-moves").status, "certified");
for (const file of [special.files.sourceFile, path.join(special.out, "entry.mjs")]) {
  const ns = await import(pathToFileURL(file));
  assert(Object.is(ns.zero, -0));
  assert.equal(ns.inverse, -Infinity);
  assert(Number.isNaN(ns.sum));
}
