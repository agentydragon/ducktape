import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, lower, workerSplit } from "./harness.mjs";

const run = workerSplit(
  "reached-strict-arguments",
  `function inspect(first, second) {
     const original = arguments;
     const read = () => arguments;
     first = 12;
     arguments[1] = 7;
     const inner = function () { return arguments; };
     const nested = inner(8);
     let poisoned = false;
     try { original.callee; } catch { poisoned = true; }
     if (!poisoned || original !== read() || nested === original || nested.length !== 1 ||
         nested[0] !== 8 || first !== 12 || second !== 3 || original[0] !== 2 ||
         original[1] !== 7 || original.length !== 3 || original[2] !== 4) throw "arguments";
     original.length = 2.5;
     const items = [...original];
     if (items.length !== 2 || items[0] !== 2 || items[1] !== 7) throw "array-like iterator";
     original.length = 3;
     return read;
   }
   export const result = (() => {
     const a = inspect(2, 3, 4), b = inspect(2, 3, 4);
     if (a() === b()) throw "reused arguments";
     return [a().length, a()[0], a()[1], b()[2]];
   })();`
);
assertCertifiedResult(run, [3, 2, 7, 4]);
const { files, out } = run;
const verify = () => lower(files.sourceFile, files.specFile, out, ["--check-output"]);
assert.equal(verify().verdict.status, "certified");
const worker = path.join(out, "worker.mjs");
const printed = readFileSync(worker, "utf8");
assert(printed.includes("arguments[1]"));
writeFileSync(worker, printed.replace("arguments[1]", "arguments[0]"));
assert.equal(verify().verdict.status, "rejected");
