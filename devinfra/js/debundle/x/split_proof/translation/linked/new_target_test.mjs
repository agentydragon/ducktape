import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, caseShard, lower, observe, sequentialSplit } from "./harness.mjs";

const runCase = caseShard();

if (runCase("constructors-and-arrows")) {
  const split = sequentialSplit(
    "new-target-constructors",
    `class Base {
      constructor(target) {
        this.baseTarget = new.target;
        this.prefixTarget = target;
      }
    }
    class Middle extends Base {
      constructor(before = new.target) {
        const read = () => new.target;
        super(read());
        this.before = before;
        this.after = () => new.target;
      }
    }
    class Leaf extends Middle {}
    export const result = (() => {
      const direct = new Base(0);
      const derived = new Leaf();
      if (direct.baseTarget !== Base || derived.baseTarget !== Leaf) return false;
      if (derived.prefixTarget !== Leaf || derived.before !== Leaf || derived.after() !== Leaf) return false;
      return true;
    })();`,
    { "base.mjs": ["#0"], "entry.mjs": ["#1", "#2", "#3"] },
    ["base.mjs"]
  );
  assertCertifiedResult(split, true);
  assert.deepEqual(
    lower(split.files.sourceFile, split.files.specFile, split.out, ["--check-output"]).verdict,
    split.verdict
  );

  const base = path.join(split.out, "base.mjs");
  const printed = readFileSync(base, "utf8");
  assert.match(printed, /new\.target/);
  writeFileSync(base, printed.replace("new.target", "Base"));
  assert.equal(
    lower(split.files.sourceFile, split.files.specFile, split.out, ["--check-output"]).verdict.status,
    "rejected"
  );
  assert.deepEqual(observe(path.join(split.out, "entry.mjs"), "result"), { result: false });
}

if (runCase("arguments-only")) {
  const split = sequentialSplit(
    "new-target-in-super-arguments",
    `class Base { constructor(target) { this.target = target; } }
    class Middle extends Base { constructor() { super(new.target); } }
    class Leaf extends Middle {}
    export const result = new Leaf().target === Leaf;`,
    { "base.mjs": ["#0"], "entry.mjs": ["#1", "#2", "#3"] },
    ["base.mjs"]
  );
  assertCertifiedResult(split, true);
}

if (runCase("ordinary-function-boundary")) {
  const split = sequentialSplit(
    "new-target-ordinary-function-boundary",
    `function Callable() { return new.target; }
    class Constructor {
      constructor() {
        function nested() { return new.target; }
        this.inner = nested();
        this.outer = (() => new.target)();
      }
    }
    export const result = Callable() === undefined && new Callable() === Callable &&
      new Constructor().inner === undefined && new Constructor().outer === Constructor;`,
    { "base.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
    ["base.mjs"]
  );
  assertCertifiedResult(split, true);
}
