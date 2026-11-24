import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { lower, prepare, root } from "./harness.mjs";

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

// The hoisted function points into the pivot's region at link time.
// Nobody can reach it during this crossing, but the suffix calls it.
const hiddenObserver = fixture(
  "isolated-hoisted-observer",
  `const bytes = new Uint8Array([9]);
   var later = () => 7;
   function observe() { return later(); }
   const result = observe() + bytes[0];
   export { bytes, later, observe, result };`,
  ["#1"],
  ["#0", "#2", "#3", "#4"]
);
const hidden = hiddenObserver.run(pivotScope);
assert.equal(hidden.status, "certified", JSON.stringify(hidden));
assert.deepEqual(hiddenObserver.run(pivotScope, "--check-output"), hidden);
for (const file of [hiddenObserver.sourceFile, path.join(hiddenObserver.out, "entry.mjs")]) {
  const { result, observe } = await import(pathToFileURL(file));
  assert.equal(result, 16);
  assert.equal(observe(), 7);
}

// A getter reaches the hoisted observer and records the pivot's type in a
// mutable binding. Moving the initializer would record "function".
const reachedObserver = fixture(
  "getter-reaches-hoisted-observer",
  `var seen = "";
   const before = { get value() { observe(); return 0; } }.value;
   var later = () => 7;
   function observe() { seen = typeof later; }
   export { before, seen, later, observe };`,
  ["#2"],
  ["#0", "#1", "#3", "#4"]
);
assert.equal((await import(pathToFileURL(reachedObserver.sourceFile))).seen, "undefined");
const reached = reachedObserver.run(refusalScope);
assert.equal(reached.status, "rejected", JSON.stringify(reached));
assert.match(reached.reason, /linked state already exposes the region of unit 2/);
assert(!existsSync(reachedObserver.out));

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

const deferredPair = fixture(
  "adjacent-closure-and-captured-initializer",
  `const bytes = new Uint8Array([17]);
   var inspect = value => read(value);
   var read = value => value + 1;
   export { bytes, inspect, read };`,
  ["#1", "#2"],
  ["#0", "#3"]
);
const pair = deferredPair.run(movesScope);
assert.equal(pair.status, "certified", JSON.stringify(pair));
assert.deepEqual(deferredPair.run(movesScope, "--check-output"), pair);
for (const file of [deferredPair.sourceFile, path.join(deferredPair.out, "entry.mjs")]) {
  const { bytes, inspect } = await import(pathToFileURL(file));
  assert.equal(inspect(bytes[0]), 18);
}

const reentry = fixture(
  "getter-observes-captured-initializer",
  `function peek() { return typeof read; }
   const receiver = { get value() { return peek(); } };
   const before = receiver.value;
   var inspect = () => read();
   var read = () => 7;
   export { before, inspect, read };`,
  ["#3", "#4"],
  ["#0", "#1", "#2", "#5"]
);
assert.equal((await import(pathToFileURL(reentry.sourceFile))).before, "undefined");
const observed = reentry.run(refusalScope);
assert.equal(observed.status, "rejected", JSON.stringify(observed));
assert.match(observed.reason, /linked state already exposes the region of unit 4/);
assert(!existsSync(reentry.out));

const hoist = fixture(
  "hoist-before-early-alias",
  `const saved = read;
   const scale = 3;
   function read(value) { return value * scale; }
   export { saved, read as convert, scale };`,
  ["#2"],
  ["#0", "#1", "#3"]
);
const hoisted = hoist.run(pivotScope);
assert.equal(hoisted.status, "certified");
assert.deepEqual(hoist.run(pivotScope, "--check-output"), hoisted);
for (const file of [hoist.sourceFile, path.join(hoist.out, "entry.mjs")]) {
  const exports = await import(pathToFileURL(file));
  assert.deepEqual(Object.keys(exports), ["convert", "saved", "scale"]);
  assert.equal(exports.saved, exports.convert);
  assert.equal(exports.convert(5), 15);
}

// Importing a private capture from the entry would add it to the entry's namespace.
const hiddenCapture = fixture(
  "private-entry-capture",
  `const hidden = 3;
   function read() { return hidden; }
   export { read };`,
  ["#1"],
  ["#0", "#2"]
);
const exposed = hiddenCapture.run(refusalScope);
assert.equal(exposed.status, "rejected");
assert.match(exposed.reason, /entry exports resolve to different linked cells/);
assert(!existsSync(hiddenCapture.out));

// An entry-side assignment would write an immutable import after the split.
const reassigned = fixture(
  "reassigned-function",
  `const saved = read;
   function read() { return 1; }
   read = () => 2;
   export { saved, read };`,
  ["#1"],
  ["#0", "#2", "#3"]
);
const immutable = reassigned.run(refusalScope);
assert.equal(immutable.status, "rejected");
assert.match(immutable.reason, /placement or module linkage/);
assert(!existsSync(reassigned.out));

// The function can move, but moving its caller would read a captured const in its TDZ.
const tdz = fixture(
  "hoist-and-call",
  `const scale = 3;
   function read() { return scale; }
   const value = read();
   export { scale, value };`,
  ["#1", "#2"],
  ["#0", "#3"]
);
const call = tdz.run(refusalScope);
assert.equal(call.status, "rejected");
assert.match(call.reason, /unit 2 is not a local allocating declaration/);
assert(!existsSync(tdz.out));

const regexRecord = fixture(
  "move-regexp-record",
  String.raw`const RegExp = () => { throw "shadowed constructor"; };
   var patterns = { first: /\s/gu, second: /\s/gu, flag: /\p{RI}\p{RI}/u };
   export { RegExp, patterns };`,
  ["#1"],
  ["#0", "#2"]
);
const record = regexRecord.run(pivotScope);
assert.equal(record.status, "certified", JSON.stringify(record));
assert.deepEqual(regexRecord.run(pivotScope, "--check-output"), record);
for (const file of [regexRecord.sourceFile, path.join(regexRecord.out, "entry.mjs")]) {
  const { patterns } = await import(pathToFileURL(file));
  assert.equal(Object.getPrototypeOf(patterns.first), RegExp.prototype);
  assert.equal(patterns.first.flags, "gu");
  assert(patterns.flag.test("🇨🇿"));
  assert.notEqual(patterns.first, patterns.second);
  patterns.first.lastIndex = 4;
  assert.equal(patterns.second.lastIndex, 0);
}

const profile = path.join(root, "browser-page.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
// Moving these initializers ahead of the patch would change the exported result.
for (const [name, patch, value] of [
  ["constructor", 'globalThis.RegExp = class { constructor() { throw "patched"; } }', 'new RegExp("a")'],
  ["getter", 'Object.defineProperty(RegExp.prototype, "flags", { get() { return "patched"; } })', "/a/g.flags"],
  ["method", "RegExp.prototype.test = () => false", '/a/.test("a")'],
]) {
  const effect = fixture(
    "regexp-record-" + name,
    `${patch}; var patterns = { literal: /a/, result: ${value} }; export { patterns };`,
    ["#1"],
    ["#0", "#2"]
  );
  const denied = effect.run(refusalScope, "--host-contract", profile);
  assert.equal(denied.status, "rejected", JSON.stringify(denied));
  assert.match(denied.reason, /unit 1 is not a local allocating declaration/);
  assert(!existsSync(effect.out));
}
