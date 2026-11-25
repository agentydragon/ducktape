import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, assertRejected, caseShard, lower, observe, sequentialSplit } from "./harness.mjs";

const runCase = caseShard();

if (runCase("both-branches")) {
  const split = sequentialSplit(
    "conditional-super-both-branches",
    `const trace = { value: "" };
    class Base {
      constructor(first, second) {
        trace.value += "b";
        this.first = first;
        this.second = second;
      }
    }
    class Child extends Base {
      field = trace.value += "f";
      constructor(input) {
        const marker = "x";
        if ((trace.value += "t", input.fast)) super((trace.value += marker, input.value));
        else {
          const local = input.options;
          super(local.value, ...local.extra);
        }
        trace.value += "p";
      }
    }
    export const result = (() => {
      const fast = new Child({ fast: true, value: 3 });
      if (fast.first !== 3 || fast.second !== undefined || fast.field !== "txbf") return false;
      const slow = new Child({ get options() {
        trace.value += "o";
        return { get value() { trace.value += "v"; return 4; }, extra: [5] };
      } });
      return slow.first === 4 && slow.second === 5 && slow.field === "txbfptovbf" &&
        trace.value === "txbfptovbfp";
    })();`,
    { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3"] },
    ["base.mjs"]
  );
  assertCertifiedResult(split, true);
  assert.deepEqual(
    lower(split.files.sourceFile, split.files.specFile, split.out, ["--check-output"]).verdict,
    split.verdict
  );

  const entry = path.join(split.out, "entry.mjs");
  const printed = readFileSync(entry, "utf8");
  assert.match(printed, /super\(local\.value/);
  writeFileSync(entry, printed.replace("super(local.value", "super(0"));
  assert.equal(
    lower(split.files.sourceFile, split.files.specFile, split.out, ["--check-output"]).verdict.status,
    "rejected"
  );
  assert.deepEqual(observe(entry, "result"), { result: false });
}

if (runCase("throwing-argument")) {
  const split = sequentialSplit(
    "conditional-super-throwing-argument",
    `const trace = { value: "" };
    class Base { constructor() { trace.value += "b"; } }
    class Child extends Base {
      field = trace.value += "f";
      constructor(fast, input) {
        if (fast) super();
        else {
          const local = input;
          super(local.fail);
        }
        trace.value += "p";
      }
    }
    export const result = (() => {
      try { new Child(false, { get fail() { trace.value += "x"; throw 3; } }); }
      catch (error) { if (error !== 3) throw error; }
      return trace.value;
    })();`,
    { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3"] },
    ["base.mjs"]
  );
  assertCertifiedResult(split, "x");
}

if (runCase("missing-branch")) {
  const split = sequentialSplit(
    "conditional-super-missing-branch",
    `class Base {}
    class Child extends Base { constructor(run) { if (run) super(); } }
    export const result = new Child(false);`,
    { "base.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
    ["base.mjs"]
  );
  assertRejected(split, /expected a direct super call/);
  assert.deepEqual(observe(split.files.sourceFile, "result"), { error: "ReferenceError" });
}
