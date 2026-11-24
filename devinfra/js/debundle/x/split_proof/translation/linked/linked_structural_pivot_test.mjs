import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { lower, prepare, root } from "./linked_programs.mjs";

const pivotScope = "linked-structural-pivot";
const movesScope = "linked-structural-moves";
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
   const read = (position) => bytes[position];
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
const reordered = multipleMoves.run(movesScope);
assert.equal(reordered.status, "certified", JSON.stringify(reordered));
assert.equal(reordered.guarantee, "normal-completion");
assert.deepEqual(multipleMoves.run(movesScope, "--check-output"), reordered);

for (const file of [safe.sourceFile, path.join(safe.out, "entry.mjs"), path.join(multipleMoves.out, "entry.mjs")]) {
  const namespace = await import(pathToFileURL(file));
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

const laterCapture = fixture(
  "previously-moved-closure-captures-next-region",
  `const first = 1;
   var observe = () => later;
   const result = observe() + first;
   var later = 7;
   export { observe, result, later };`,
  ["#1", "#3"],
  ["#0", "#2", "#4"]
);
const capture = laterCapture.run(refusalScope);
assert.equal(capture.status, "rejected", JSON.stringify(capture));
assert.match(capture.reason, /unit 1 has access to the moved region of unit 3/);
assert(!existsSync(laterCapture.out));
