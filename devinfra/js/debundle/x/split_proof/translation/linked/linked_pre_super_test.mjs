import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, assertRejected, lower, observe, sequentialSplit } from "./linked_programs.mjs";

const source = `
  let trace = "";
  const mark = (step, value) => { trace += step; return value; };
  class Base {
    constructor(message, context) {
      trace += "base;";
      return { message, context };
    }
  }
  class Child extends Base {
    constructor(recordId, collectionId) {
      const message = \`Record \${recordId} is unavailable\`;
      let nextId = mark("prefix;", recordId);
      nextId = nextId + 1;
      (super(mark("message;", message), mark("context;", { recordId: nextId, collectionId })),
       this.recordId = recordId, this.localMessage = message, this.nextId = nextId);
      mark("body;");
    }
  }
  const value = new Child(12, "archive");
  const checked = (() => {
    if (value.message !== "Record 12 is unavailable" || value.localMessage !== value.message) throw 1;
    if (value.context.recordId !== 13 || value.context.collectionId !== "archive") throw 2;
    if (value.recordId !== 12 || value.nextId !== 13) throw 3;
    if (trace !== "prefix;message;context;base;body;") throw 4;
    return true;
  })();
  export const result = checked;
`;
const split = sequentialSplit(
  "pre-super-prefix",
  source,
  { "base.mjs": ["#0", "#1", "#2"], "child.mjs": ["#3"], "entry.mjs": ["#4", "#5", "#6"] },
  ["base.mjs", "child.mjs"]
);
assertCertifiedResult(split, true);
const checked = lower(split.files.sourceFile, split.files.specFile, split.out, ["--check-output"]);
assert.deepEqual(checked.verdict, split.verdict, checked.log);

const childPath = path.join(split.out, "child.mjs");
const original = readFileSync(childPath, "utf8");
const changed = original.replace("is unavailable", "could not be read");
assert.notEqual(changed, original);
writeFileSync(childPath, changed);
const disk = lower(split.files.sourceFile, split.files.specFile, split.out, ["--check-output"]);
assert.equal(disk.verdict.status, "rejected", disk.log);

const tdz = sequentialSplit(
  "pre-super-tdz",
  `const latest = 1;
  class Base {}
  class Child extends Base {
    constructor() {
      const first = latest;
      super(first + 1);
      const latest = 3;
    }
  }
  export const result = new Child();`,
  { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3"] },
  ["base.mjs"]
);
assertRejected(tdz, /original threw during linked initialization/);
assert.deepEqual(observe(tdz.files.sourceFile, "result"), { error: "ReferenceError" });

// Super arguments run before the superclass constructor check, even with a null base.
const nullBase = sequentialSplit(
  "pre-super-null-base",
  `const trace = { value: "" };
  class Child extends null {
    constructor() {
      trace.value += "prefix;";
      super(trace.value += "first;", trace.value += "second;");
    }
  }
  export const result = (() => {
    try { new Child(); }
    catch (error) {
      if (typeof error === "object" && trace.value === "prefix;first;second;") return trace.value;
      throw error;
    }
    throw 1;
  })();`,
  { "state.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
  ["state.mjs"]
);
assertCertifiedResult(nullBase, "prefix;first;second;");

for (const [name, body] of [
  ["nested-receiver", "const x = 1 + this.x; super(x);"],
  ["captured-receiver", "const read = () => this.x; super(read);"],
  ["argument-receiver", "super(this.x);"],
]) {
  const refused = sequentialSplit(
    `pre-super-${name}`,
    `const Child = class extends null { constructor() { ${body} } }; export const result = Child;`,
    { "child.mjs": ["#0"], "entry.mjs": ["#1"] },
    ["child.mjs"]
  );
  assertRejected(refused, /before super/i);
}
