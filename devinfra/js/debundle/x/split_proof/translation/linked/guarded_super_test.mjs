import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, assertRejected, caseShard, lower, observe, sequentialSplit } from "./harness.mjs";

const runCase = caseShard();

if (runCase("guard-and-assignment-sequence")) {
  const run = sequentialSplit(
    "guarded-super-assignment-sequence",
    `const trace = { value: "" };
    const mark = (event, value) => { trace.value += event; return value; };
    class Base {
      constructor(value) { mark("b"); return { base: value }; }
    }
    class Child extends Base {
      field = mark("f", 10);
      constructor(fail, ...items) {
        const seed = mark("s", items[0]);
        if (mark("t", fail)) throw mark("x", seed);
        const chosen = mark("l", seed + 1);
        (super(mark("a", chosen)),
         this.total = mark("p", this.base + this.field + seed),
         this.chosen = mark("q", chosen));
      }
    }
    export const result = (() => {
      let caught = false;
      try { new Child(true, 7); }
      catch (error) { caught = error === 7; }
      if (!caught || trace.value !== "stx") return false;
      const child = new Child(false, 2);
      return trace.value === "stxstlabfpq" && child.total === 15 && child.chosen === 3;
    })();`,
    { "base.mjs": ["#0", "#1", "#2"], "entry.mjs": ["#3", "#4"] },
    ["base.mjs"]
  );
  assertCertifiedResult(run, true);
  assert.deepEqual(lower(run.files.sourceFile, run.files.specFile, run.out, ["--check-output"]).verdict, run.verdict);

  const entry = path.join(run.out, "entry.mjs");
  const printed = readFileSync(entry, "utf8");
  const mutant = printed.replace('mark("q", chosen)', 'mark("q", 0)');
  assert.notEqual(mutant, printed);
  writeFileSync(entry, mutant);
  assert.equal(lower(run.files.sourceFile, run.files.specFile, run.out, ["--check-output"]).verdict.status, "rejected");
  assert.deepEqual(observe(entry, "result"), { result: false });
}

if (runCase("braced-guard-and-return-sequence")) {
  const run = sequentialSplit(
    "guarded-super-return-sequence",
    `class Base { constructor(value) { this.value = value; } }
    class Child extends Base {
      field = 3;
      constructor(fail, value) {
        if (fail) { throw value; }
        return (super(value), this.total = this.value + this.field, { result: this.total });
      }
    }
    export const result = new Child(false, 2).result;`,
    { "base.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
    ["base.mjs"]
  );
  assertCertifiedResult(run, 5);
}

if (runCase("unmodeled-early-return")) {
  const run = sequentialSplit(
    "guarded-super-unmodeled-early-return",
    `class Base {}
    class Child extends Base {
      constructor(flag) {
        if (flag) return { value: 5 };
        super();
      }
    }
    export const result = new Child(true).value;`,
    { "base.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
    ["base.mjs"]
  );
  assertRejected(run, /unsupported IfStatement before super/);
  assert.deepEqual(observe(run.files.sourceFile, "result"), { result: 5 });
}
