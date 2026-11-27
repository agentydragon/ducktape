import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { assertRejectedOrOrderKeptSlice, caseShard, structuralFixture, workerSplit } from "./harness.mjs";

const runCase = caseShard();

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

if (runCase("reached-async-generator")) {
  assertRejectedOrOrderKeptSlice(
    workerSplit("reached-async-generator", "async function* run() { yield 1; } export const result = run();"),
    /async generator invocation requires asynchronous suspension semantics/
  );
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
