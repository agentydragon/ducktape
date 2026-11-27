import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, lower, sequentialSplit } from "./harness.mjs";

const entry = "entry.mjs";

const eagerSubtraction = sequentialSplit(
  "eager-subtraction",
  "const length = 5; const result = length - 1; export { result };",
  undefined,
  undefined,
  "",
  "indexed-local-exports"
);
assertCertifiedResult(eagerSubtraction, 4);
assert.equal(eagerSubtraction.verdict.strategy, "compiled-constructor", eagerSubtraction.log);
assert.equal(eagerSubtraction.verdict.guarantee, "all-outcomes", eagerSubtraction.log);
assert.deepEqual(
  lower(
    eagerSubtraction.files.sourceFile,
    eagerSubtraction.files.specFile,
    eagerSubtraction.out,
    ["--check-output"],
    "indexed-local-exports"
  ).verdict,
  eagerSubtraction.verdict
);
const eagerEntry = path.join(eagerSubtraction.out, entry);
const eagerBytes = readFileSync(eagerEntry, "utf8");
const wrongOperator = eagerBytes.replace("length - 1", "length + 1");
assert.notEqual(wrongOperator, eagerBytes);
writeFileSync(eagerEntry, wrongOperator);
const wrongOperatorCheck = lower(
  eagerSubtraction.files.sourceFile,
  eagerSubtraction.files.specFile,
  eagerSubtraction.out,
  ["--check-output"],
  "indexed-local-exports"
);
assert.equal(wrongOperatorCheck.verdict.status, "rejected", wrongOperatorCheck.log);
assert.equal(readFileSync(eagerEntry, "utf8"), wrongOperator);
