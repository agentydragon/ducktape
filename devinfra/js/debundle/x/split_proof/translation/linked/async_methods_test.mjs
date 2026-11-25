import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { assertRejected, assertRejectedOrOrderKeptSlice, lower, sequentialSplit, workerSplit } from "./harness.mjs";

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

assertRejectedOrOrderKeptSlice(
  workerSplit("reached-async-method", "const box = { async run() { return 1; } }; export const value = box.run();"),
  /deferred function run: async invocation requires Promise semantics/
);
assertRejectedOrOrderKeptSlice(
  workerSplit(
    "reached-async-super-method",
    "class Box { async read() { return super.read(); } } export const value = new Box().read();"
  ),
  /deferred function read: async invocation requires Promise semantics/
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
   function factory() { return async () => class { #field; }; }
   export { before, read, arrow, named, factory };`,
  { "offset.mjs": ["#0"], "funcs.mjs": ["#1", "#2", "#3", "#4", "#5"], "entry.mjs": ["#6"] },
  ["offset.mjs", "funcs.mjs"]
);
assert.equal(functions.verdict.status, "certified", functions.log);
for (const file of [functions.files.sourceFile, path.join(functions.out, "entry.mjs")]) {
  const { before, read, arrow, named, factory } = await import(pathToFileURL(file).href);
  // The enclosing function can retain the complete syntax, including the private
  // name. Its nested refusal must compare equal after relocation.
  assert.equal(typeof (await factory()()), "function");
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
// A never-called definition can fail during lowering, before the execution probe.
const definition = workerSplit(
  "unretained-async-definition",
  "async function locate() { return import.meta.url; } export const value = 1;"
);
assertRejected(definition, /cannot retain async function definition at unit 0, callable 1: .*\(MetaProperty\)/);
assert.equal(definition.verdict.reason_code, "linked_input", definition.log);

const invocation = workerSplit(
  "reached-async-function",
  "async function run() { return 1; } export const value = run();"
);
const orderKept = assertRejectedOrOrderKeptSlice(
  invocation,
  /deferred function run: async invocation requires Promise semantics/
);
if (!orderKept) assert.equal(invocation.verdict.reason_code, "probe_unknown", invocation.log);
