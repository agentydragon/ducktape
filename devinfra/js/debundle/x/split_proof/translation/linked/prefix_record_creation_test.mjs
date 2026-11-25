import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { assertRejectedResultChange, observe, structuralSplit } from "./harness.mjs";

function fixture(name, source, moved, kept) {
  return structuralSplit(name, source, { "records.mjs": moved, "entry.mjs": kept });
}

const ok = fixture(
  "fresh-prefix-records",
  'const marker=0; var key="__proto__"; var label="active"; var first={[key]:100, label:label}; var second={[key]:100,label:label}; export {first,second};',
  ["#1", "#2", "#3", "#4"],
  ["#0", "#5"]
);
const verdict = ok.run("linked-structural-moves");
assert.equal(verdict.status, "certified");
assert.deepEqual(ok.run("linked-structural-moves", "--check-output"), verdict);
for (const f of [ok.sourceFile, path.join(ok.out, "entry.mjs")]) {
  const { first, second } = await import(pathToFileURL(f));
  assert.notEqual(first, second);
  assert.equal(first.label, "active");
  assert.equal(Object.getPrototypeOf(first), Object.prototype);
  assert.deepEqual(Object.getOwnPropertyDescriptor(first, "__proto__"), {
    value: 100,
    writable: true,
    configurable: true,
    enumerable: true,
  });
  first.label = "changed";
  assert.equal(second.label, "active");
}

for (const [name, source, moved, kept, before, after] of [
  [
    "mutated-key",
    'let key="a"; const change=()=>{key="b"}; change(); var result={[key]:1}; export {key,change,result};',
    ["#0", "#1", "#3"],
    ["#2", "#4"],
    { result: { b: 1 } },
    { result: { a: 1 } },
  ],
  [
    "coercing-key",
    'Object.prototype.toString=()=>"patched"; const key={}; var result={[key]:2}; export {result};',
    ["#1", "#2"],
    ["#0", "#3"],
    { result: { patched: 2 } },
    { result: { "[object Object]": 2 } },
  ],
  [
    "early-value",
    'const label="a"; var result={label:label}; export {label,result};',
    ["#1"],
    ["#0", "#2"],
    { result: { label: "a" } },
    { error: "ReferenceError" },
  ],
]) {
  assertRejectedResultChange(fixture(name, source, moved, kept), before, after);
}

// The earlier table supplies an own field read. The retained allocation is
// crossed by that read; only the source prefix knows its key at that point.
const retained = fixture(
  "retained-computed-key",
  'globalThis.marker = 0; var key="k"; var table={value:1}; var other={[key]:2}; var result=table.value; export {key,table,other,result};',
  ["#1", "#2", "#4"],
  ["#0", "#3", "#5"]
);
const retainedVerdict = retained.run("linked-structural-moves");
assert.equal(retainedVerdict.status, "certified");
assert.deepEqual(retained.run("linked-structural-moves", "--check-output"), retainedVerdict);
for (const f of [retained.sourceFile, path.join(retained.out, "entry.mjs")]) {
  assert.deepEqual(observe(f, ["result", "other"]), { result: 1, other: { k: 2 } });
}
