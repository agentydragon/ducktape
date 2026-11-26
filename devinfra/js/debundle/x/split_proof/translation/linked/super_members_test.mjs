import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  assertCertifiedResult,
  assertRejected,
  assertRejectedOrOrderKeptSlice,
  caseShard,
  forcePreview,
  lower,
  observe,
  sequentialSplit,
  workerSplit,
} from "./harness.mjs";

const runCase = caseShard();

if (runCase("inherited-receivers-and-disk")) {
  const inherited = sequentialSplit(
    "super-members",
    `const run = (callback) => callback();
   class Base {
     get value() { return this.label; }
     get show() {
       this.label += " getter";
       return function (arg) { return this.label + " " + arg; };
     }
     static describe() { return this.label; }
   }
   class Middle extends Base {
     label = "receiver";
     show() { return "override"; }
     callback() { return () => run(() => [super.value, (super["show"])(this.label), super.absent?.(this.label = "wrong")]); }
     static describe() { return super.describe(); }
     async deferred() { return run(() => super.value); }
   }
   class Leaf extends Middle {}
   Leaf.label = "static receiver";
   const instance = new Leaf();
   const copied = { label: "borrowed", callback: instance.callback };
   const callback = copied.callback();
   export const result = [callback(), Leaf.describe(), instance.show(), copied.label];
   export { Leaf };`,
    { "base.mjs": ["#0", "#1"], "middle.mjs": ["#2"], "entry.mjs": ["#3", "#4", "#5", "#6", "#7", "#8", "#9"] },
    ["base.mjs", "middle.mjs"]
  );
  assertCertifiedResult(inherited, [
    ["borrowed", "borrowed getter borrowed getter", null],
    "static receiver",
    "override",
    "borrowed getter",
  ]);
  assert.deepEqual(
    lower(inherited.files.sourceFile, inherited.files.specFile, inherited.out, ["--check-output"]).verdict,
    inherited.verdict
  );
  for (const file of [inherited.files.sourceFile, path.join(inherited.out, "entry.mjs")]) {
    const { Leaf } = await import(pathToFileURL(file).href);
    assert.equal(await new Leaf().deferred(), "receiver");
  }
  const middleFile = path.join(inherited.out, "middle.mjs");
  const middle = readFileSync(middleFile, "utf8");
  const changed = middle.replace("super.value", "this.value");
  assert.notEqual(changed, middle);
  writeFileSync(middleFile, changed);
  assert.equal(
    lower(inherited.files.sourceFile, inherited.files.specFile, inherited.out, ["--check-output"]).verdict.status,
    "rejected"
  );
}

if (runCase("object-method")) {
  const object = workerSplit(
    "super-object-method",
    `const reporter = {
     __proto__: { report() { return this.value; }, get value() { return this.payload; } },
     payload: 9,
     nested: { __proto__: { report() { return 6; } }, report() { return super.report(); } },
     report() { return [super.report(), super.value, this.nested.report()]; },
   };
   export const result = reporter.report();`
  );
  assertCertifiedResult(object, [9, 9, 6]);
}

if (runCase("install-and-read-order")) {
  const ordered = `class Base {}
  class Child extends Base { read() { return super.read?.(); } }
  Base.prototype.read = function () { return this.value; };
  const instance = new Child();
  instance.value = "ready";
  const result = instance.read() ?? "missing";
  export { result };`;
  const programs = {
    "classes.mjs": ["#0", "#1", "#3", "#4"],
    "install.mjs": ["#2"],
    "read.mjs": ["#5"],
    "entry.mjs": ["#6"],
  };
  const ready = ["classes.mjs", "install.mjs", "read.mjs"];
  const early = ["classes.mjs", "read.mjs", "install.mjs"];
  assertCertifiedResult(sequentialSplit("super-install-first", ordered, programs, ready), "ready");
  const changedOrder = sequentialSplit("super-read-first", ordered, programs, early);
  assertRejected(changedOrder, /probe executions completed normally with different final states/);
  const preview = path.join(changedOrder.dir, "preview");
  forcePreview(changedOrder.files, preview);
  assert.deepEqual(observe(path.join(preview, "entry.mjs"), "result"), { result: "missing" });
}

if (runCase("null-home-prototype")) {
  const nullBase = workerSplit(
    "super-null-home-prototype",
    `const object = { __proto__: null, read() { return super.value; } };
   export const result = object.read();`
  );
  assertRejectedOrOrderKeptSlice(nullBase, /original threw during linked initialization/);
  assert.deepEqual(observe(nullBase.files.sourceFile, "result"), { error: "TypeError" });
}
