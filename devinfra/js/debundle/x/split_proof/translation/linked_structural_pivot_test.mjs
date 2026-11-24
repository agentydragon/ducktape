import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { lower, prepare, root } from "./testing/linked_programs.mjs";

const scope = "linked-structural-pivot";

function fixture(name, source, pivot, kept, provider = "") {
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source,
    provider,
    spec: {
      entry: "entry.mjs",
      modules: { "entry.mjs": kept, "scroll.mjs": [pivot] },
    },
  });
  const out = path.join(dir, "out");
  const run = (...args) =>
    lower(files.sourceFile, files.specFile, out, ["--structural-pivot", pivot.slice(1), ...args], scope).verdict;
  return { ...files, out, run };
}

const safe = fixture(
  "move-across-typed-array",
  `import { seed } from "./provider.mjs";
   const bytes = new Uint8Array([seed, 17]);
   function read(position) { return bytes[position]; }
   var scroll = ({ amount }) => amount + 1;
   const answer = read(1);
   export { scroll as Bg, bytes, answer };`,
  "#3",
  ["#0", "#1", "#2", "#4", "#5"],
  "export const seed = 9;"
);

const certified = safe.run();
assert.equal(certified.status, "certified", JSON.stringify(certified));
assert.equal(certified.guarantee, "normal-completion");
assert.deepEqual(safe.run("--check-output"), certified);

const reorderedOut = path.join(root, "order-preserving-rejects-a-move");
const reordered = lower(
  safe.sourceFile,
  safe.specFile,
  reorderedOut,
  ["--structural-order"],
  "linked-structural-order"
);
assert.equal(reordered.verdict.status, "rejected");
assert.match(reordered.verdict.reason, /the linked schedules differ/);
assert(!existsSync(reorderedOut));

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
const changed = safe.run("--check-output");
assert.equal(changed.status, "rejected");
assert.match(changed.reason, /differs from its generated ESTree/);

const earlyRead = fixture(
  "moved-region-in-initial-heap",
  `function callScroll() { return scroll({ amount: 1 }); }
   const original = typeof scroll;
   var scroll = ({ amount }) => amount + 1;
   export { callScroll, original, scroll as Bg };`,
  "#2",
  ["#0", "#1", "#3"]
);
const refused = earlyRead.run();
assert.equal(refused.status, "rejected", JSON.stringify(refused));
assert.match(refused.reason, /linked state already exposes the region of unit 2/);
assert(!existsSync(earlyRead.out));
