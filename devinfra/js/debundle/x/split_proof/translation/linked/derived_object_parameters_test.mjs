import assert from "node:assert/strict";
import { assertCertifiedResult, lower, sequentialSplit } from "./harness.mjs";

const checked = sequentialSplit(
  "derived-object-parameter",
  `const trace = { value: "" };
  class Base { constructor(x) { trace.value += "s"; this.value = x; } }
  class Child extends Base {
    field = trace.value += "f";
    constructor({ first: x, second: y }) { trace.value += "p"; super(x + y); }
  }
  export const result = (() => {
    const good = { get first() { trace.value += "a"; return 2; }, get second() { trace.value += "b"; return 3; } };
    const value = new Child(good).value;
    try { new Child({ get first() { trace.value += "x"; throw 7; }, get second() { trace.value += "!"; } }); }
    catch (e) { if (e !== 7) throw e; }
    if (trace.value !== "abpsfx" || value !== 5 || Child.length !== 1) throw "parameter order";
    return trace.value;
  })();`,
  { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3"] },
  ["base.mjs"]
);
assertCertifiedResult(checked, "abpsfx");
assert.deepEqual(
  lower(checked.files.sourceFile, checked.files.specFile, checked.out, ["--check-output"]).verdict,
  checked.verdict
);
