// `reduceLines`: delta debugging finds the lines a property needs, keeps the lines it is told to, and tries few candidates.
import assert from "node:assert/strict";
import { reduceLines } from "./footprint_reduce.mjs";

const lines = Array.from({ length: 24 }, (_, i) => `unit ${i}`);
let calls = 0;
const needs =
  (...wanted) =>
  async (candidate) => {
    calls++;
    return wanted.every((w) => candidate.includes(w));
  };

assert.deepEqual(await reduceLines(lines, needs("unit 3", "unit 17")), ["unit 3", "unit 17"]);
assert(calls < 24 * 3, `too many candidates tried: ${calls}`);
// a line that must stay is kept even when the property does not need it
assert.deepEqual(await reduceLines(lines, needs("unit 3"), new Set([10])), ["unit 3", "unit 10"]);
// nothing can be removed: the whole list comes back
assert.deepEqual(await reduceLines(["a", "b"], needs("a", "b")), ["a", "b"]);
// a property that needs a pair of neighbours in order is still found
assert.deepEqual(
  await reduceLines(["x", "a", "y", "b", "z"], async (c) => c.indexOf("a") >= 0 && c.indexOf("b") > c.indexOf("a")),
  ["a", "b"]
);
console.log("footprint reduce ok");
