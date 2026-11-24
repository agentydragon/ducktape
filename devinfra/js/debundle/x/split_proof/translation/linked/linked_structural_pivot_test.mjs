import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { lower, prepare, root } from "./linked_programs.mjs";

const pivotScope = "linked-structural-pivot";
const refusalScope = "linked-structural";

function fixture(name, source, moved, kept, provider = "") {
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source,
    provider,
    spec: {
      entry: "entry.mjs",
      modules: { "entry.mjs": kept, "scroll.mjs": moved },
    },
  });
  const out = path.join(dir, "out");
  const run = (scope, ...args) =>
    lower(files.sourceFile, files.specFile, out, ["--structural", ...args], scope).verdict;
  return { sourceFile: files.sourceFile, out, run };
}

const typedArraySource = `import { seed } from "./provider.mjs";
   const bytes = new Uint8Array([seed, 17]);
   function read(position) { return bytes[position]; }
   var scroll = ({ amount }) => amount + 1;
   const answer = read(1);
   export { scroll as Bg, bytes, answer };`;

const safe = fixture(
  "move-across-typed-array",
  typedArraySource,
  ["#3"],
  ["#0", "#1", "#2", "#4", "#5"],
  "export const seed = 9;"
);

const certified = safe.run(pivotScope);
assert.equal(certified.status, "certified", JSON.stringify(certified));
assert.equal(certified.guarantee, "normal-completion");
assert.deepEqual(safe.run(pivotScope, "--check-output"), certified);

const multipleMoves = fixture(
  "multiple-moves",
  typedArraySource,
  ["#2", "#3"],
  ["#0", "#1", "#4", "#5"],
  "export const seed = 9;"
);
const reordered = multipleMoves.run(refusalScope);
assert.equal(reordered.status, "rejected");
assert.match(reordered.reason, /the linked schedules do not differ by a single earlier move/);
assert(!existsSync(multipleMoves.out));

const entryFile = path.join(safe.out, "entry.mjs");
const original = await import(pathToFileURL(safe.sourceFile));
const emitted = await import(pathToFileURL(entryFile));
for (const namespace of [original, emitted]) {
  assert.deepEqual(Object.keys(namespace), ["Bg", "answer", "bytes"]);
  assert.equal(namespace.answer, 17);
  assert.equal(namespace.Bg({ amount: 22 }), 23);
  assert(namespace.bytes instanceof Uint8Array);
  assert.deepEqual([...namespace.bytes], [9, 17]);
}

const worker = path.join(safe.out, "scroll.mjs");
writeFileSync(worker, readFileSync(worker, "utf8").replace("amount + 1", "amount + 2"));
const changed = safe.run(refusalScope, "--check-output");
assert.equal(changed.status, "rejected");
assert.match(changed.reason, /differs from its generated ESTree/);

const earlyRead = fixture(
  "moved-region-in-initial-heap",
  `function callScroll() { return scroll({ amount: 1 }); }
   const original = typeof scroll;
   var scroll = ({ amount }) => amount + 1;
   export { callScroll, original, scroll as Bg };`,
  ["#2"],
  ["#0", "#1", "#3"]
);
const refused = earlyRead.run(refusalScope);
assert.equal(refused.status, "rejected", JSON.stringify(refused));
assert.match(refused.reason, /linked state already exposes the region of unit 2/);
assert(!existsSync(earlyRead.out));
