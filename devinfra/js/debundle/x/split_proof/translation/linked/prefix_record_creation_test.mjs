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
    spec: { entry: "entry.mjs", modules: { "records.mjs": moved, "entry.mjs": kept } },
  });
  const out = path.join(dir, "out");
  return {
    files,
    out,
    run: (scope, ...flags) => lower(files.sourceFile, files.specFile, out, ["--structural", ...flags], scope).verdict,
  };
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
for (const f of [ok.files.sourceFile, path.join(ok.out, "entry.mjs")]) {
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
  const test = fixture(name, source, moved, kept);
  assert.equal(test.run("linked-structural").status, "rejected");
  assert(!existsSync(test.out));
  const preview = path.join(root, name, "preview");
  forcePreview(test.files, preview);
  assert.deepEqual(observe(test.files.sourceFile, "result"), before);
  assert.deepEqual(observe(path.join(preview, "entry.mjs"), "result"), after);
}
