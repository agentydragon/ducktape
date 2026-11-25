import assert from "node:assert/strict";
import { assertCertifiedResult, assertRejectedOrOrderKeptSlice, lower, observe, workerSplit } from "./harness.mjs";

const copied = workerSplit(
  "object-spread",
  `const source = { id: 1, get label() { return this.id; } };
   class Row {
     get value() { return { ...source, id: 2 }; }
   }
   const copy = { ...new Row().value, ...null, label: 3 };
   export const result = [source.id, copy.id, copy.label];`,
  ["#1", "#2", "#3"]
);
assertCertifiedResult(copied, [1, 2, 3]);
assert.deepEqual(
  lower(copied.files.sourceFile, copied.files.specFile, copied.out, ["--check-output"]).verdict,
  copied.verdict
);

const opaque = workerSplit(
  "object-spread-error",
  `const run = () => { try { null.x; } catch (err) { return { ...err }; } };
   export const result = run();`
);
assertRejectedOrOrderKeptSlice(opaque, /object spread inspects an engine error/);

const supplementary = workerSplit(
  "object-spread-supplementary",
  `const run = () => ({ ..."A😀" });
   export const result = run();`
);
assert.deepEqual(observe(supplementary.files.sourceFile, "result"), {
  result: { 0: "A", 1: "\ud83d", 2: "\ude00" },
});
assertRejectedOrOrderKeptSlice(supplementary, /object spread would copy an isolated UTF-16 surrogate/);
