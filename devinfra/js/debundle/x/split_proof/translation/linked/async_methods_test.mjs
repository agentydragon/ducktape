import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { assertRejected, lower, sequentialSplit, workerSplit } from "./harness.mjs";

const source = `
  const offset = 7;
  class Box {
    async read(x, y = offset, ...rest) { return this.value + await x + y + rest.length; }
    static async echo({ x }, ...rest) { return x + rest.length; }
  }
  export const methods = { Box, async other() { return offset; } };`;
const test = sequentialSplit(
  "deferred-async-methods",
  source,
  { "offset.mjs": ["#0"], "box.mjs": ["#1"], "entry.mjs": ["#2"] },
  ["offset.mjs", "box.mjs"]
);
assert.equal(test.verdict.status, "certified", test.log);
assert.deepEqual(lower(test.files.sourceFile, test.files.specFile, test.out, ["--check-output"]).verdict, test.verdict);

const intrinsic = Object.getPrototypeOf(async () => {});
for (const file of [test.files.sourceFile, path.join(test.out, "entry.mjs")]) {
  const { methods } = await import(pathToFileURL(file).href);
  const { Box, other } = methods;
  const box = new Box();
  box.value = 3;
  for (const [fn, name, length] of [
    [box.read, "read", 1],
    [Box.echo, "echo", 1],
    [other, "other", 0],
  ]) {
    assert.equal(Object.getPrototypeOf(fn), intrinsic);
    assert.equal(fn.name, name);
    assert.equal(fn.length, length);
    assert.equal(Object.hasOwn(fn, "prototype"), false);
    assert.throws(() => new fn(), TypeError);
  }
  assert.equal(await box.read(2), 12);
  assert.equal(await Box.echo({ x: 8 }, 1), 9);
  assert.equal(await other(), 7);
}

// A body can be deferred only while the emitted module retains its code.
const boxFile = path.join(test.out, "box.mjs");
const original = readFileSync(boxFile, "utf8");
const changed = original.replace("await x", "await 99");
assert.notEqual(original, changed);
writeFileSync(boxFile, changed);
assert.equal(
  lower(test.files.sourceFile, test.files.specFile, test.out, ["--check-output"]).verdict.status,
  "rejected"
);

assertRejected(
  workerSplit("reached-async-method", "const box = { async run() { return 1; } }; export const value = box.run();"),
  /deferred function run: async invocation requires Promise semantics/
);
assertRejected(
  workerSplit(
    "async-method-needs-home",
    "class Box { async read() { return super.read(); } } export const value = Box;"
  ),
  /async invocation/
);

// An async declaration is callable before its declaration is evaluated. Reading
// it during initialization is modeled; await the exported callables later in Node.
const functions = sequentialSplit(
  "deferred-async-functions",
  `const offset = 2;
   const before = read;
   async function read(x, y = offset) { return await x + y; }
   const arrow = async (...xs) => (await read(xs[0])) + offset;
   const named = async function inner(x) { return x ? inner(0) : offset; };
   export { before, read, arrow, named };`,
  { "offset.mjs": ["#0"], "funcs.mjs": ["#1", "#2", "#3", "#4"], "entry.mjs": ["#5"] },
  ["offset.mjs", "funcs.mjs"]
);
assert.equal(functions.verdict.status, "certified", functions.log);
for (const file of [functions.files.sourceFile, path.join(functions.out, "entry.mjs")]) {
  const { before, read, arrow, named } = await import(pathToFileURL(file).href);
  assert.equal(before, read);
  for (const [fn, name, length] of [
    [read, "read", 1],
    [arrow, "arrow", 0],
    [named, "inner", 1],
  ]) {
    assert.equal(Object.getPrototypeOf(fn), intrinsic);
    assert.equal(fn.name, name);
    assert.equal(fn.length, length);
    assert.throws(() => new fn(), TypeError);
  }
  assert.deepEqual(await Promise.all([read(3), arrow(4), named(1)]), [5, 8, 2]);
}
assertRejected(
  workerSplit("reached-async-function", "async function run() { return 1; } export const value = run();"),
  /deferred function run: async invocation requires Promise semantics/
);
