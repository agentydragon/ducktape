import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  assertRejectedOrOrderKeptSlice,
  caseShard,
  lower,
  sequentialSplit,
  structuralFixture,
  workerSplit,
} from "./harness.mjs";

const runCase = caseShard();

if (runCase("generator-creation-and-disk")) {
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
  assert.equal(test.verdict.scope, "linked-programs", test.log);
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
}

for (const [name, definition, called] of [
  ["declaration", "function* run() { yield 1; }", "run()"],
  ["expression-default", "const run = function* (value = (() => { throw 1; })()) { yield value; };", "run()"],
  ["method", "const box = { *run() { yield 1; } };", "box.run()"],
]) {
  if (!runCase(`reached-generator-${name}`)) continue;
  assertRejectedOrOrderKeptSlice(
    workerSplit(`reached-generator-${name}`, `${definition} export const result = ${called};`),
    /generator invocation requires suspension semantics/
  );
}

if (runCase("async-generator-creation")) {
  const asyncSource = `
  async function* stream(value, fallback = 8) { yield value; yield fallback; }
  const box = { async *values() { yield 12; } };
  const another = async function* () { yield 13; };
  function* sync() {}
  export const result = (() => {
    if (stream.length !== 1 || stream.name !== "stream" || another.name !== "another") throw "name or length";
    if (stream.prototype === box.values.prototype || stream.prototype === sync.prototype) throw "own prototype";
    if (stream.prototype.constructor === sync.prototype.constructor) throw "function kind";
    if (stream.prototype.constructor !== another.prototype.constructor) throw "shared prototype";
    try { new stream(); } catch (err) { return true; }
    return false;
  })();
  export { stream, box, another };`;
  const asyncTest = workerSplit("async-generator-creation", asyncSource, ["#1", "#2", "#3", "#4", "#5"]);
  assert.equal(asyncTest.verdict.status, "certified", asyncTest.log);
  const checkedAsync = lower(asyncTest.files.sourceFile, asyncTest.files.specFile, asyncTest.out, ["--check-output"]);
  assert.deepEqual(checkedAsync.verdict, asyncTest.verdict, checkedAsync.log);
  const intrinsicAsyncGenerator = async function* () {};
  for (const file of [asyncTest.files.sourceFile, path.join(asyncTest.out, "entry.mjs")]) {
    const { result, stream, box, another } = await import(pathToFileURL(file).href);
    assert.equal(result, true);
    for (const fn of [stream, box.values, another]) {
      assert.equal(Object.getPrototypeOf(fn), Object.getPrototypeOf(intrinsicAsyncGenerator));
      assert.equal(Object.getPrototypeOf(fn.prototype), Object.getPrototypeOf(intrinsicAsyncGenerator.prototype));
    }
    const iterator = stream(7);
    assert.deepEqual(await iterator.next(), { value: 7, done: false });
    assert.deepEqual(await iterator.next(), { value: 8, done: false });
  }
}

if (runCase("reached-async-generator")) {
  assertRejectedOrOrderKeptSlice(
    workerSplit("reached-async-generator", "async function* run() { yield 1; } export const result = run();"),
    /async generator invocation requires asynchronous suspension semantics/
  );
}

if (runCase("retained-generators")) {
  // Both declarations stay in the entry; only an unrelated initializer moves.
  // The alias is read before the declaration and the captured cell is written
  // between resumes, after both entry graphs have finished initializing.
  const spelling =
    "function* stream(value, fallback = 8) { /* keep me */ yield value; yield current; yield fallback; }";
  const retained = structuralFixture(
    "retained-generators",
    `const alias = stream;
   ${spelling}
   async function* asyncStream() { yield current; }
   let current = 1;
   function update(value) { current = value; }
   const unrelated = 11;
   export { alias, stream, asyncStream, update, unrelated };`,
    ["#5"],
    ["#0", "#1", "#2", "#3", "#4", "#6"]
  );
  const moved = retained.run("linked-structural-pivot");
  assert.equal(moved.status, "certified", JSON.stringify(moved));
  assert.deepEqual(retained.run("linked-structural-pivot", "--check-output"), moved);
  const entryFile = path.join(retained.out, "entry.mjs");
  for (const file of [retained.sourceFile, entryFile]) {
    const { alias, stream, asyncStream, update, unrelated } = await import(pathToFileURL(file).href);
    assert.equal(unrelated, 11);
    assert.equal(alias, stream);
    assert.equal(Function.prototype.toString.call(alias), spelling);
    assert.notEqual(stream.prototype, asyncStream.prototype);
    for (const [fn, intrinsic, length, name] of [
      [stream, function* () {}, 1, "stream"],
      [asyncStream, async function* () {}, 0, "asyncStream"],
    ]) {
      assert.equal(fn.length, length);
      assert.equal(fn.name, name);
      assert.equal(Object.getPrototypeOf(fn), Object.getPrototypeOf(intrinsic));
      assert.equal(Object.getPrototypeOf(fn.prototype), Object.getPrototypeOf(intrinsic.prototype));
      assert.equal(Object.hasOwn(fn.prototype, "constructor"), false);
      assert.throws(() => new fn(), TypeError);
    }
    const iterator = alias(2);
    assert.deepEqual(iterator.next(), { value: 2, done: false });
    update(23);
    assert.deepEqual(iterator.next(), { value: 23, done: false });
    assert.deepEqual(iterator.next(), { value: 8, done: false });
    assert.deepEqual(await asyncStream().next(), { value: 23, done: false });
  }

  const reparsed = readFileSync(entryFile, "utf8");
  assert(reparsed.includes(spelling));
  writeFileSync(entryFile, reparsed.replace("/* keep me */", "/* different spelling */"));
  assert.equal(retained.run("linked-structural", "--check-output").status, "rejected");
}

if (runCase("retained-generator-observes-initializer")) {
  const reached = structuralFixture(
    "retained-generator-observes-initializer",
    `function* observe() { yield later; }
   const before = observe().next().value;
   var later = 7;
   export { before, later, observe };`,
    ["#2"],
    ["#0", "#1", "#3"]
  );
  assert.equal((await import(pathToFileURL(reached.sourceFile).href)).before, undefined);
  const denied = reached.run("linked-structural");
  assert.equal(denied.status, "rejected", JSON.stringify(denied));
  assert.match(denied.reason, /already exposes the region of unit 2/);
}
