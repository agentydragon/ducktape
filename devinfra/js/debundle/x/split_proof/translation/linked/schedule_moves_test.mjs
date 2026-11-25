import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parse } from "acorn";
import { structuralFixture } from "./harness.mjs";

const pivotScope = "linked-structural-pivot";
const movesScope = "linked-structural-moves";
const refusalScope = "linked-structural";

// Creating the class stores the initializer; only construction reads `later`.
const instanceField = structuralFixture(
  "move-class-with-instance-field",
  `const before = 3;
   const C = class { value = later(); };
   function later() { return 7; }
   export { before, C, later };`,
  ["#1"],
  ["#0", "#2", "#3"]
);
const instanceFieldVerdict = instanceField.run(pivotScope);
assert.equal(instanceFieldVerdict.status, "certified", JSON.stringify(instanceFieldVerdict));
assert.deepEqual(instanceField.run(pivotScope, "--check-output"), instanceFieldVerdict);
for (const file of [instanceField.sourceFile, path.join(instanceField.out, "entry.mjs")]) {
  const { before, C } = await import(pathToFileURL(file));
  assert.equal(before, 3);
  assert.equal(new C().value, 7);
}

const staticField = structuralFixture(
  "move-class-with-static-field",
  `const before = 3;
   const C = class { static value = { answer: 7 }; static owner = this; };
   export { before, C };`,
  ["#1"],
  ["#0", "#2"]
);
const staticFieldVerdict = staticField.run(pivotScope);
assert.equal(staticFieldVerdict.status, "certified", JSON.stringify(staticFieldVerdict));
assert.deepEqual(staticField.run(pivotScope, "--check-output"), staticFieldVerdict);
for (const file of [staticField.sourceFile, path.join(staticField.out, "entry.mjs")]) {
  const { before, C } = await import(pathToFileURL(file));
  assert.equal(before, 3);
  assert.equal(C.value.answer, 7);
  assert.equal(C.owner, C);
}

const eagerField = structuralFixture(
  "class-static-field-must-not-cross-binding",
  `const before = 3;
   const C = class { static value = before; };
   export { before, C };`,
  ["#1"],
  ["#0", "#2"]
);
const eagerFieldVerdict = eagerField.run(refusalScope);
assert.equal(eagerFieldVerdict.status, "rejected");
assert.match(eagerFieldVerdict.reason, /unit 1 is not a local allocating declaration/);

const opaqueContext = structuralFixture(
  "retained-dynamic-import-needs-module-context",
  `const load = () => import("./lazy.mjs");
   const phase = 1;
   export { load, phase };`,
  ["#1"],
  ["#0", "#2"]
);
const opaqueContextVerdict = opaqueContext.run(refusalScope);
assert.equal(opaqueContextVerdict.status, "rejected", JSON.stringify(opaqueContextVerdict));
assert.match(opaqueContextVerdict.reason, /opaque unit requires source-module or method context/);
assert.equal(opaqueContextVerdict.reason_code, "structural_opaque_unit_context");
assert(!existsSync(opaqueContext.out));

const typedArraySource = `import { seed } from "./provider.mjs";
   const bytes = new Uint8Array([seed, 17]);
   const read = (position) => bytes[position];
   var scroll = ({ amount }) => amount + 1;
   const answer = read(1);
   export { scroll as Bg, bytes, answer };`;

const safe = structuralFixture(
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

const multipleMoves = structuralFixture(
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

const laterCapture = structuralFixture(
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
assert.equal(capture.reason_code, "structural_moved_region_access");
assert(!existsSync(laterCapture.out));

const deferredPair = structuralFixture(
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

// The helper and its two interleaved declarations are assigned together.
const interleaved = structuralFixture(
  "ordered-forward-capture",
  `const bytes = new Uint8Array([7]);
   var inspect = x => read(x);
   var offset = () => 2;
   var scale = x => offset() * x;
   var read = x => x + 1;
   export { bytes, inspect, offset, scale, read };`,
  ["#1", "#2", "#3", "#4"],
  ["#0", "#5"]
);
const group = interleaved.run(movesScope);
assert.equal(group.status, "certified", JSON.stringify(group));
assert.deepEqual(interleaved.run(movesScope, "--check-output"), group);
const emittedGroup = parse(readFileSync(path.join(interleaved.out, "scroll.mjs"), "utf8"), {
  sourceType: "module",
  ecmaVersion: "latest",
});
assert.deepEqual(
  emittedGroup.body.flatMap((node) => (node.declaration ?? node).declarations?.map((d) => d.id.name) ?? []),
  ["inspect", "offset", "scale", "read"]
);
for (const file of [interleaved.sourceFile, path.join(interleaved.out, "entry.mjs")]) {
  const { bytes, inspect, scale } = await import(pathToFileURL(file));
  assert.equal(inspect(scale(bytes[0])), 15);
}

// An actual read by a hoisted function during the crossing is observable,
// whether entered directly or by property access. Neither permits the group.
for (const access of ["peek()", "receiver.value"]) {
  const active = structuralFixture(
    access === "peek()" ? "group-crosses-call" : "group-crosses-getter",
    `function peek() { return typeof read; }
     const receiver = { get value() { return peek(); } };
     const before = ${access};
     var inspect = () => read();
     var independent = () => 3;
     var read = () => 7;
     export { before, inspect, independent, read };`,
    ["#3", "#4", "#5"],
    ["#0", "#1", "#2", "#6"]
  );
  assert.equal((await import(pathToFileURL(active.sourceFile))).before, "undefined");
  const refused = active.run(refusalScope);
  assert.equal(refused.status, "rejected", JSON.stringify(refused));
  assert.match(refused.reason, /linked state already exposes the region of unit 5/);
  assert(!existsSync(active.out));
}

const reentry = structuralFixture(
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

// The same retained closure is harmless to the first pivot but can read the
// second. Its linked capture stays fixed; the region to exclude changes.
const retainedReader = `var observe = () => later;
   const before = observe();
   var independent = 1;
   var later = 2;
   export { before, independent, later };`;
const one = structuralFixture("retained-capture-one-pivot", retainedReader, ["#2"], ["#0", "#1", "#3", "#4"]);
const oneVerdict = one.run(pivotScope);
assert.equal(oneVerdict.status, "certified", JSON.stringify(oneVerdict));
assert.deepEqual(one.run(pivotScope, "--check-output"), oneVerdict);
for (const file of [one.sourceFile, path.join(one.out, "entry.mjs")]) {
  const values = await import(pathToFileURL(file));
  assert.equal(values.before, undefined);
  assert.equal(values.later, 2);
}
const two = structuralFixture("retained-capture-two-pivots", retainedReader, ["#2", "#3"], ["#0", "#1", "#4"]);
const twoVerdict = two.run(refusalScope);
assert.equal(twoVerdict.status, "rejected", JSON.stringify(twoVerdict));
assert.match(twoVerdict.reason, /unit 0 has access to the moved region of unit 3/);
assert.equal(twoVerdict.reason_code, "structural_moved_region_access");
assert(!existsSync(two.out));
