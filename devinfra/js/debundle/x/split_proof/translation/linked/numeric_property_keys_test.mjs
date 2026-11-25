import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, caseShard, lower, workerSplit } from "./harness.mjs";

const includesCase = caseShard();

if (includesCase("fractional-property-keys")) {
  const split = workerSplit(
    "fractional-property-keys",
    `const record = { 5e-1: "half", "5e-1": "literal", 1.5: "one and a half" };
  export const result = (() => {
    if (record[0.5] !== "half" || record["0.5"] !== "half" || record["5e-1"] !== "literal" ||
        record[1.5] !== "one and a half" || record["1.5"] !== "one and a half") {
      throw "incorrect property key";
    }
    return record;
  })();`
  );
  assertCertifiedResult(split, { 0.5: "half", "5e-1": "literal", 1.5: "one and a half" });

  const { files, out } = split;
  const checkOutput = () => lower(files.sourceFile, files.specFile, out, ["--check-output"]);
  assert.deepEqual(checkOutput().verdict, split.verdict);

  const worker = path.join(out, "worker.mjs");
  const original = readFileSync(worker, "utf8");
  const changed = original.replace(/(?:5e-1|0\.5)(?=\s*:\s*["']half["'])/, "0.6");
  assert.notEqual(changed, original);
  writeFileSync(worker, changed);
  const rejected = checkOutput();
  assert.equal(rejected.verdict.status, "rejected", rejected.log);
  assert.match(rejected.verdict.reason, /worker\.mjs differs from its generated ESTree/, rejected.log);
}

if (includesCase("rounded-property-keys")) {
  const boundaries = workerSplit(
    "rounded-property-keys",
    `const record = { 0.1: "tenth", 1e21: "exponent", 9007199254740993: "rounded", 1e309: "overflow" };
  export const result = (() => {
    if (record["0.1"] !== "tenth" || record["1e+21"] !== "exponent" || record["9007199254740992"] !== "rounded" ||
        record["Infinity"] !== "overflow") {
      throw "incorrect property key";
    }
    return record;
  })();`
  );
  assertCertifiedResult(boundaries, {
    0.1: "tenth",
    "1e+21": "exponent",
    9007199254740992: "rounded",
    Infinity: "overflow",
  });
}
