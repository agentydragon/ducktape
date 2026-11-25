import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { assertCertifiedResult, lower, sequentialSplit, workerSplit } from "./harness.mjs";

assertCertifiedResult(
  workerSplit(
    "static-block-null-heritage",
    `class Child extends null { static { this.value = 1; } }
     export const result = Child.value;`
  ),
  1
);

const effects = sequentialSplit(
  "static-blocks-and-fields",
  `const trace = { value: "" };
   const mark = (note, value) => { trace.value += note; return value; };
   class Base {
     static label = "parent";
     static describe() { return this.seen; }
   }
   const Child = class Named extends Base {
     static first = mark("first;", "early");
     static {
       var local = 8;
       this.seen = mark("block;", super.label + this.first);
       this.inner = () => Named;
       this.saved = () => local;
     }
     static later = mark("last;", super.describe());
     static {
       var local = 6;
       mark("second block;", local);
     }
   };
   export const result = [trace.value, Child.later, Child.inner() === Child, Child.saved()];
   export { Child };`,
  { "base.mjs": ["#0", "#1", "#2"], "entry.mjs": ["#3", "#4", "#5"] },
  ["base.mjs"]
);
assertCertifiedResult(effects, ["first;block;last;second block;", "parentearly", true, 8]);
assert.deepEqual(
  lower(effects.files.sourceFile, effects.files.specFile, effects.out, ["--check-output"]).verdict,
  effects.verdict
);
for (const file of [effects.files.sourceFile, path.join(effects.out, "entry.mjs")]) {
  const { Child } = await import(pathToFileURL(file).href);
  assert.deepEqual(Reflect.ownKeys(Child), ["length", "name", "prototype", "first", "seen", "inner", "saved", "later"]);
}

// Even a block textually above a computed instance-field key must wait for that key.
const tdz = sequentialSplit(
  "static-block-after-computed-keys",
  `const trace = { value: "" };
   const computed = () => {
     try {
       const skipped = class { static { trace.value += "incorrect;"; } [key] = 1; };
       return skipped;
     } catch (error) {
       trace.value += "key threw;";
     }
     const key = "instance";
     return trace.value;
   };
   export const result = computed();`,
  { "trace.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
  ["trace.mjs"]
);
assertCertifiedResult(tdz, "key threw;");

// An abrupt block completion skips all remaining static initialization.
const abrupt = sequentialSplit(
  "throwing-static-block",
  `const trace = { value: "" };
   const compute = () => {
     try {
       const skipped = class {
         static prior = (trace.value += "field;");
         static { trace.value += "throw;"; throw "stop"; }
         static after = (trace.value += "late;");
       };
       return skipped;
     } catch (error) {
       return error + ":" + trace.value;
     }
   };
   export const result = compute();`,
  { "trace.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
  ["trace.mjs"]
);
assertCertifiedResult(abrupt, "stop:field;throw;");
