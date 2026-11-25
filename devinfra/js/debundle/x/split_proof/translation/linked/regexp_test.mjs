import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, assertRejectedOrOrderKeptSlice, lower, workerSplit } from "./harness.mjs";

const source = String.raw`const make = () => /[a/]+\s/ymi;
const check = () => {
  const a = make(), b = make();
  if (a === b || a.lastIndex !== 0 || b.lastIndex !== 0) throw "identity";
  a.lastIndex = 4;
  if (a.lastIndex !== 4 || b.lastIndex !== 0 || {...a}.lastIndex !== undefined) throw "lastIndex";
  if (a.source !== "[a/]+\\s" || a.flags !== "imy" || !a.sticky || a.global) throw "slots";
  return true;
};
export const result = check();`;
const test = workerSplit("regexp-literal", source, ["#1", "#2"]);
assertCertifiedResult(test, true);

const file = path.join(test.out, "worker.mjs");
const original = readFileSync(file, "utf8");
for (const changed of [original.replace("[a/]", "[b/]"), original.replace("ymi", "ym")]) {
  assert.notEqual(changed, original);
  writeFileSync(file, changed);
  const disk = lower(test.files.sourceFile, test.files.specFile, test.out, ["--check-output"]);
  assert.equal(disk.verdict.status, "rejected", disk.log);
}
for (const method of ["exec", "test"]) {
  const matcher = workerSplit("regexp-" + method, `const rx = /a/g; export const result = rx.${method}("a");`);
  assertRejectedOrOrderKeptSlice(matcher, new RegExp("RegExp\\.prototype\\." + method));
}
