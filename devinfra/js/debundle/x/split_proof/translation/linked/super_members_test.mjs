import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import {
  assertLinkedPreviewResult,
  assertMatchedModel,
  prepareLinkedFixture,
  prepareWorkerFixture,
} from "./fixture_preview.mjs";
import { caseShard, moduleEvaluationOrder, observe } from "./harness.mjs";

function sequentialFixture(name, source, modules, order) {
  return prepareLinkedFixture(name, source, { entry: "entry.mjs", modules, order, sequential: true });
}

const runCase = caseShard();

if (runCase("inherited-receivers-and-disk")) {
  const inherited = sequentialFixture(
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
  const { sourceFile, specFile, manifest, out } = inherited;
  const expected = [
    ["borrowed", "borrowed getter borrowed getter", null],
    "static receiver",
    "override",
    "borrowed getter",
  ];
  assertPublicEmissionRefused(manifest, out);
  await withSplitPreview({ manifest }, async ({ directory, entryFile }) => {
    for (const file of [sourceFile, entryFile]) {
      assert.deepEqual(observe(file, "result"), { result: expected });
      const { Leaf } = await import(pathToFileURL(file).href);
      assert.equal(await new Leaf().deferred(), "receiver");
    }
    assertMatchedModel(inherited, directory, [
      [["result", "0", "0"], "borrowed"],
      [["result", "0", "1"], "borrowed getter borrowed getter"],
      [["result", "0", "2"], undefined],
      [["result", "1"], "static receiver"],
      [["result", "2"], "override"],
      [["result", "3"], "borrowed getter"],
    ]);

    const middleFile = path.join(directory, "middle.mjs");
    const middle = readFileSync(middleFile, "utf8");
    const changed = middle.replace("super.value", "this.value");
    assert.notEqual(changed, middle);
    writeFileSync(middleFile, changed);
    const model = probeLinkedModel({ source: sourceFile, spec: specFile, directory });
    assert.equal(model.status, "refused", JSON.stringify(model));
    assert.equal(model.reason_code, "linked_input", JSON.stringify(model));
    assert.match(model.reason, /emitted syntax differs from original unit 2/);
    assertPublicEmissionRefused(manifest, directory);
    assertPublicRecheckRefused(manifest, directory);
  });
}

if (runCase("object-method")) {
  const object = prepareWorkerFixture(
    "super-object-method",
    `const reporter = {
     __proto__: { report() { return this.value; }, get value() { return this.payload; } },
     payload: 9,
     nested: { __proto__: { report() { return 6; } }, report() { return super.report(); } },
     report() { return [super.report(), super.value, this.nested.report()]; },
   };
   export const result = reporter.report();`
  );
  assertPublicEmissionRefused(object.manifest, object.out);
  await assertLinkedPreviewResult(
    object,
    [9, 9, 6],
    [
      [["result", "0"], 9],
      [["result", "1"], 9],
      [["result", "2"], 6],
    ]
  );
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
  const good = sequentialFixture("super-install-first", ordered, programs, ready);
  assertPublicEmissionRefused(good.manifest, good.out);
  await assertLinkedPreviewResult(good, "ready");
  const { manifest, out, sourceFile, specFile } = sequentialFixture("super-read-first", ordered, programs, early);
  assertPublicEmissionRefused(manifest, out);
  assert.deepEqual(observe(sourceFile, "result"), { result: "ready" });
  await withSplitPreview({ manifest, expectedExit: 1 }, ({ directory, entryFile }) => {
    assert.deepEqual(moduleEvaluationOrder(directory, "entry.mjs"), [...early, "entry.mjs"]);
    assert.deepEqual(observe(entryFile, "result"), { result: "missing" });
    const model = probeLinkedModel({ source: sourceFile, spec: specFile, directory, observe: [["result"]] });
    assert.equal(model.status, "refused", JSON.stringify(model));
    assert.equal(model.reason_code, "probe_mismatch", JSON.stringify(model));
    assert.deepEqual(model.observation, {
      kind: "value",
      path: ["result"],
      original: '"ready"',
      split: '"missing"',
    });
  });
}

if (runCase("null-home-prototype")) {
  const nullBase = prepareWorkerFixture(
    "super-null-home-prototype",
    `const object = { __proto__: null, read() { return super.value; } };
   export const result = object.read();`
  );
  const { manifest, out, sourceFile, specFile } = nullBase;
  assertPublicEmissionRefused(manifest, out);
  assert.deepEqual(observe(sourceFile, "result"), { error: "TypeError" });
  await withSplitPreview({ manifest }, ({ directory, entryFile }) => {
    assert.deepEqual(observe(entryFile, "result"), { error: "TypeError" });
    const model = probeLinkedModel({ source: sourceFile, spec: specFile, directory });
    assert.equal(model.status, "refused", JSON.stringify(model));
    assert.equal(model.reason_code, "probe_throw", JSON.stringify(model));
    assert.match(model.reason, /original threw during linked initialization/);
  });
}
