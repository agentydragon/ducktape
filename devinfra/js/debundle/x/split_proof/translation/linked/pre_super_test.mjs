import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, assertRejectedOrOrderKeptSlice, lower, observe, sequentialSplit } from "./harness.mjs";

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
assertRejectedOrOrderKeptSlice(tdz, /original threw during linked initialization/);
assert.deepEqual(observe(tdz.files.sourceFile, "result"), { error: "ReferenceError" });

const vars = sequentialSplit(
  "pre-super-var",
  `const trace = { value: "" };
   class Base { constructor(value) { trace.value += value + ";"; } }
   class Child extends Base {
     constructor(value) {
       trace.value += typeof scratch + ";";
       var value, scratch = value + 1;
       (super(value), this.scratch = scratch);
     }
   }
   class Defaults extends Base {
     constructor(value = 3) {
       var scratch = value + 2;
       if ((super(scratch), scratch === 5)) this.scratch = scratch;
     }
   }
   const a = new Child(7);
   const b = new Defaults();
   export const result = [trace.value, a.scratch, b.scratch];`,
  { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3", "#4", "#5", "#6"] },
  ["base.mjs"]
);
assertCertifiedResult(vars, ["undefined;7;5;", 8, 5]);

const defaultShadow = sequentialSplit(
  "pre-super-var-default-shadow",
  `const source = 7;
   class Base { constructor(value) { this.value = value; } }
   class Child extends Base {
     constructor(value = source) { var source = 9; super(value); }
   }
   export const result = new Child().value;`,
  { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3"] },
  ["base.mjs"]
);
assertRejectedOrOrderKeptSlice(defaultShadow, /var before super overlaps default parameters or their references/);
assert.deepEqual(observe(defaultShadow.files.sourceFile, "result"), { result: 7 });

const functionOverlap = sequentialSplit(
  "pre-super-var-function-overlap",
  `class Base { constructor(value) { this.value = value; } }
   class Child extends Base {
     constructor() { var same = 6; super(same); function same() {} }
   }
   export const result = new Child().value;`,
  { "base.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
  ["base.mjs"]
);
assertRejectedOrOrderKeptSlice(functionOverlap, /local function declaration overlaps a var binding before super/);
assert.deepEqual(observe(functionOverlap.files.sourceFile, "result"), { result: 6 });

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

const continuation = sequentialSplit(
  "super-expression-continuation",
  `const trace = { value: "" };
   class Base { constructor() { trace.value += "base;"; return { value: "replacement" }; } }
   class Child extends Base {
     field = "initialized";
     constructor(flag) {
       if ((super(), trace.value += this.field + ";", flag)) trace.value += this.value + ";";
       else trace.value += "else;";
     }
   }
   class Returned extends Base {
     field = "returned";
     constructor() { return super(); }
   }
   class ReturningExpression extends Base {
     field = "evaluated";
     constructor() { return (super(), trace.value += this.field + ";", { wrapped: this }); }
   }
   new Child(true);
   new Child(false);
   const returned = new Returned();
   const expression = new ReturningExpression();
   export const result = [trace.value, returned.field, returned.value, expression.wrapped.value];`,
  { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3", "#4", "#5", "#6", "#7", "#8", "#9"] },
  ["base.mjs"]
);
assertCertifiedResult(continuation, [
  "base;initialized;replacement;base;initialized;else;base;base;evaluated;",
  "returned",
  "replacement",
  "replacement",
]);
assert.deepEqual(
  lower(continuation.files.sourceFile, continuation.files.specFile, continuation.out, ["--check-output"]).verdict,
  continuation.verdict
);

const commaPrefix = sequentialSplit(
  "super-comma-prefix",
  `const trace = { value: "" };
   const mark = (step, value) => { trace.value += step; return value; };
   const check = (ok) => { if (!ok) throw 5; };
   class Base { constructor(value) { trace.value += value; } }
   class Child extends Base {
     field = "field;";
     constructor(ok) {
       if ((mark("before;"), check(ok), super(mark("argument;", "base;")), mark(this.field), ok))
         mark("body;");
     }
   }
   new Child(true);
   try { new Child(false); } catch (error) { if (error !== 5) throw error; mark("caught;"); }
   export const result = trace.value;`,
  { "base.mjs": ["#0", "#1", "#2", "#3"], "entry.mjs": ["#4", "#5", "#6", "#7"] },
  ["base.mjs"]
);
assertCertifiedResult(commaPrefix, "before;argument;base;field;body;before;caught;");
assert.deepEqual(
  lower(commaPrefix.files.sourceFile, commaPrefix.files.specFile, commaPrefix.out, ["--check-output"]).verdict,
  commaPrefix.verdict
);

const conditional = sequentialSplit(
  "conditional-super-refused",
  `class Base {}
   class Child extends Base { constructor(flag) { if (flag && super()) this.ok = true; } }
   export const result = Child;`,
  { "base.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
  ["base.mjs"]
);
assertRejectedOrOrderKeptSlice(conditional, /expected a direct super call/);

const captured = sequentialSplit(
  "pre-super-arrow-receiver",
  `const trace = { value: "" };
   class Base {
     constructor(read, value) {
       try { trace.value += read(); }
       catch (error) { trace.value += "uninitialized;"; }
       return { read, value };
     }
   }
   class Child extends Base {
     field = 9;
     constructor(prefix) {
       const read = () => typeof this;
       super(read, (() => () => prefix + this.field)());
     }
   }
   const child = new Child("late:");
   export const result = [trace.value, child.read(), child.value()];`,
  { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3", "#4"] },
  ["base.mjs"]
);
assertCertifiedResult(captured, ["uninitialized;", "object", "late:9"]);
assert.deepEqual(
  lower(captured.files.sourceFile, captured.files.specFile, captured.out, ["--check-output"]).verdict,
  captured.verdict
);

const ordinaryReceivers = sequentialSplit(
  "pre-super-ordinary-receivers",
  `class Base {
     constructor(options, fn) {
       options.value = 4;
       return { first: options.method(9), second: { value: 2, fn }.fn(13) };
     }
   }
   class Child extends Base {
     constructor(seed) {
       const makeOptions = () => ({
         method(offset) {
           const read = () => this.value + offset + seed;
           return read();
         },
       });
       super(makeOptions(), function (offset) { return this.value + offset + seed; });
     }
   }
   const child = new Child(5);
   export const result = [child.first, child.second];`,
  { "base.mjs": ["#0"], "entry.mjs": ["#1", "#2", "#3"] },
  ["base.mjs"]
);
assertCertifiedResult(ordinaryReceivers, [18, 20]);

for (const [name, body] of [
  ["nested-receiver", "const x = 1 + this.x; super(x);"],
  ["captured-super", "const read = () => super.x; super(read);"],
  ["argument-receiver", "super(this.x);"],
  ["object-method-computed-key", "super({ [this.x]() {} });"],
  ["comma-prefix-receiver", "(this.x, super());"],
]) {
  const refused = sequentialSplit(
    `pre-super-${name}`,
    `const Child = class extends null { constructor() { ${body} } }; export const result = Child;`,
    { "child.mjs": ["#0"], "entry.mjs": ["#1"] },
    ["child.mjs"]
  );
  assertRejectedOrOrderKeptSlice(refused, /before super/i);
}
