import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import {
  assertLinkedPreviewResult,
  assertMatchedModel,
  prepareLinkedFixture,
  prepareWorkerFixture,
} from "./fixture_preview.mjs";
import { observe } from "./harness.mjs";

function sequentialFixture(name, source, modules, order) {
  return prepareLinkedFixture(name, source, { entry: "entry.mjs", modules, order, sequential: true });
}

const nullHeritage = prepareWorkerFixture(
  "static-block-null-heritage",
  `class Child extends null { static { this.value = 1; } }
   export const result = Child.value;`
);
assertPublicEmissionRefused(nullHeritage.manifest, nullHeritage.out);
await assertLinkedPreviewResult(nullHeritage, 1);

const effects = sequentialFixture(
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
const result = ["first;block;last;second block;", "parentearly", true, 8];
assertPublicEmissionRefused(effects.manifest, effects.out);
await withSplitPreview({ manifest: effects.manifest }, async ({ directory, entryFile }) => {
  for (const file of [effects.sourceFile, entryFile]) {
    assert.deepEqual(observe(file, "result"), { result });
    const { Child } = await import(pathToFileURL(file).href);
    assert.deepEqual(Reflect.ownKeys(Child), [
      "length",
      "name",
      "prototype",
      "first",
      "seen",
      "inner",
      "saved",
      "later",
    ]);
  }
  assertMatchedModel(
    effects,
    directory,
    result.map((value, index) => [["result", String(index)], value])
  );
  assertPublicEmissionRefused(effects.manifest, directory);
  assertPublicRecheckRefused(effects.manifest, directory);
});

// Even a block textually above a computed instance-field key must wait for that key.
const tdz = sequentialFixture(
  "static-block-after-computed-keys",
  `const trace = { value: "" };
   const computed = () => {
     try {
       const skipped = class { static { trace.value += "incorrect;"; } [(trace.value += "key;", key)] = 1; };
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
assertPublicEmissionRefused(tdz.manifest, tdz.out);
await assertLinkedPreviewResult(tdz, "key;key threw;");

// An abrupt block completion skips all remaining static initialization.
const abrupt = sequentialFixture(
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
assertPublicEmissionRefused(abrupt.manifest, abrupt.out);
await assertLinkedPreviewResult(abrupt, "stop:field;throw;");
