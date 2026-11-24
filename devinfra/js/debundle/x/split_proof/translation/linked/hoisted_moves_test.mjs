import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { structuralFixture } from "./harness.mjs";

const pivotScope = "linked-structural-pivot";
const refusalScope = "linked-structural";

// The hoisted function points into the pivot's region at link time.
// Nobody can reach it during this crossing, but the suffix calls it.
const hiddenObserver = structuralFixture(
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
const reachedObserver = structuralFixture(
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

// Both hoists capture the moved binding. A getter that calls either one
// before the initializer can record "undefined" instead of "function".
for (const called of [null, "first", "second"]) {
  const two = structuralFixture(
    "two-hoisted-observers-" + (called ?? "isolated"),
    `var seen = "";
     const before = ${called ? `{ get value() { ${called}(); return 9; } }.value` : "new Uint8Array([9])[0]"};
     var later = () => 7;
     function first() { seen = typeof later; return seen; }
     function second() { seen = typeof later; return seen; }
     export { before, seen, later, first, second };`,
    ["#2"],
    ["#0", "#1", "#3", "#4", "#5"]
  );
  const source = await import(pathToFileURL(two.sourceFile));
  assert.equal(source.seen, called ? "undefined" : "");
  if (called) {
    const verdict = two.run(refusalScope);
    assert.equal(verdict.status, "rejected", JSON.stringify(verdict));
    assert.match(verdict.reason, /linked state already exposes the region of unit 2/);
    assert(!existsSync(two.out));
  } else {
    const verdict = two.run(pivotScope);
    assert.equal(verdict.status, "certified", JSON.stringify(verdict));
    assert.deepEqual(two.run(pivotScope, "--check-output"), verdict);
    const emitted = await import(pathToFileURL(path.join(two.out, "entry.mjs")));
    for (const ns of [source, emitted]) {
      assert.equal(ns.seen, "");
      assert.equal(ns.before, 9);
      assert.equal(ns.first(), "function");
      assert.equal(ns.second(), "function");
      assert.equal(ns.later(), 7);
    }
  }
}

const earlyRead = structuralFixture(
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

const hoist = structuralFixture(
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
const hiddenCapture = structuralFixture(
  "private-entry-capture",
  `const hidden = 3;
   function read() { return hidden; }
   export { read };`,
  ["#1"],
  ["#0", "#2"]
);
const exposed = hiddenCapture.run(refusalScope);
assert.equal(exposed.status, "rejected");
assert.match(exposed.reason, /extra 1: "hidden" \(candidate cell @1\.0\); missing 0; retargeted 0/);
assert(!existsSync(hiddenCapture.out));

// An entry-side assignment would write an immutable import after the split.
const reassigned = structuralFixture(
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
const tdz = structuralFixture(
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
