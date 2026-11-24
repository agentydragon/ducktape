import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { assertRejected, lower, sequentialSplit, workerSplit } from "./harness.mjs";

const source = `
  const offset = 7;
  const before = read;
  function* read(first, next = offset) { yield first; yield* [next, offset + 1]; }
  const make = (value) => function* stream() { yield value + offset; };
  const left = make(2), right = make(3);
  const anonymous = function* (first, ...tail) { yield* [first, ...tail]; };
  const methods = { *run(first) { yield this.value + first; }, value: 3 };
  class Box {
    *run() { yield offset; }
    static *read(first) { yield first; }
  }
  const check = () => {
    if (before !== read || left === right || left.prototype === right.prototype) throw "identity";
    if (read.prototype === undefined || read.prototype === methods.run.prototype) throw "prototype";
    if (read.name !== "read" || read.length !== 1 || left.name !== "stream") throw "declaration and named expression";
    if (anonymous.name !== "anonymous" || anonymous.length !== 1 || methods.run.name !== "run") throw "inferred names";
    if (Box.prototype.run.prototype === undefined || Box.read.prototype === undefined) throw "class method prototypes";
    read.prototype.marker = 11;
    if (before.prototype.marker !== 11 || left.prototype.marker !== undefined) throw "own prototype";
    try { new before(); }
    catch (err) { if (typeof err === "object") return true; throw err; }
    return false;
  };
  const result = check();
  export { before, read, left, right, anonymous, methods, Box, result };`;
const test = sequentialSplit(
  "generator-creation",
  source,
  {
    "offset.mjs": ["#0"],
    "functions.mjs": ["#1", "#2", "#3", "#4", "#5", "#6", "#7", "#8", "#9", "#10"],
    "entry.mjs": ["#11"],
  },
  ["offset.mjs", "functions.mjs"]
);
assert.equal(test.verdict.status, "certified", test.log);
assert.equal(test.verdict.guarantee, "normal-completion", test.log);
const checked = lower(test.files.sourceFile, test.files.specFile, test.out, ["--check-output"]);
assert.deepEqual(checked.verdict, test.verdict, checked.log);

const generatorFunctionPrototype = Object.getPrototypeOf(function* () {});
const generatorPrototype = generatorFunctionPrototype.prototype;
for (const file of [test.files.sourceFile, path.join(test.out, "entry.mjs")]) {
  const { before, read, left, right, anonymous, methods, Box, result } = await import(pathToFileURL(file).href);
  assert.equal(result, true);
  assert.equal(before, read);
  const box = new Box();
  const prototypes = new Set();
  for (const [fn, name, length] of [
    [read, "read", 1],
    [left, "stream", 0],
    [right, "stream", 0],
    [anonymous, "anonymous", 1],
    [methods.run, "run", 1],
    [box.run, "run", 0],
    [Box.read, "read", 1],
  ]) {
    assert.equal(Object.getPrototypeOf(fn), generatorFunctionPrototype);
    for (const [property, value, writable, configurable] of [
      ["name", name, false, true],
      ["length", length, false, true],
      ["prototype", fn.prototype, true, false],
    ]) {
      assert.deepEqual(Object.getOwnPropertyDescriptor(fn, property), {
        value,
        writable,
        configurable,
        enumerable: false,
      });
    }
    assert.equal(Object.getPrototypeOf(fn.prototype), generatorPrototype);
    assert.equal(Object.hasOwn(fn.prototype, "constructor"), false);
    assert.throws(() => new fn(), TypeError);
    prototypes.add(fn.prototype);
  }
  assert.equal(prototypes.size, 7);
  assert.deepEqual([...read(2)], [2, 7, 8]);
  assert.deepEqual([...left()], [9]);
  assert.deepEqual([...right()], [10]);
  assert.deepEqual([...anonymous(1, 2, 3)], [1, 2, 3]);
  assert.deepEqual([...methods.run(4)], [7]);
  assert.deepEqual([...box.run()], [7]);
  assert.deepEqual([...Box.read(5)], [5]);
}

const functionsFile = path.join(test.out, "functions.mjs");
const original = readFileSync(functionsFile, "utf8");
const changed = original.replace("offset + 1", "offset + 2");
assert.notEqual(original, changed);
writeFileSync(functionsFile, changed);
const changedOutput = lower(test.files.sourceFile, test.files.specFile, test.out, ["--check-output"]);
assert.equal(changedOutput.verdict.status, "rejected", changedOutput.log);

for (const [name, definition, called] of [
  ["declaration", "function* run() { yield 1; }", "run()"],
  ["expression-default", "const run = function* (value = (() => { throw 1; })()) { yield value; };", "run()"],
  ["method", "const box = { *run() { yield 1; } };", "box.run()"],
]) {
  assertRejected(
    workerSplit(`reached-generator-${name}`, `${definition} export const result = ${called};`),
    /generator invocation requires suspension semantics/
  );
}

for (const [name, definition, exported] of [
  ["declaration", "async function* run() { yield 1; }", "run"],
  ["expression", "const run = async function* () { yield 1; };", "run"],
  ["method", "const box = { async *run() { yield 1; } };", "box"],
]) {
  assertRejected(
    workerSplit(`async-generator-${name}`, `${definition} export const result = ${exported};`),
    /async generator/
  );
}
