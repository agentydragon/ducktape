import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { assertLinkedPreviewResult, expectedModelObservations, prepareLinkedFixture } from "./fixture_preview.mjs";
import { observe } from "./harness.mjs";

const host = { profile: "browser-page" };

function fixture(name, source) {
  return prepareLinkedFixture(name, source, {
    entry: "entry.mjs",
    modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
    order: ["worker.mjs"],
    sequential: true,
  });
}

const premise =
  "Symbol.iterator returns the well-known iterator symbol without observable effects throughout initialization";

const method = fixture(
  "iterator-method",
  `class Collection {
    constructor(inner) { this.inner = inner; }
    [Symbol.iterator]() { return this.inner[Symbol.iterator](); }
    "Symbol.iterator"() { return "string key"; }
  }
  const result = (() => {
    const key = Symbol.iterator;
    const inner = { label: "called with inner" };
    inner[key] = function () { return this.label; };
    const wrapped = new Collection(inner);
    const spread = { ...inner };
    if (typeof key !== "symbol" || key !== Symbol.iterator || key === "Symbol.iterator") throw 1;
    if (wrapped[key]() !== "called with inner" || wrapped["Symbol.iterator"]() !== "string key") throw 2;
    if (wrapped[key].name !== "[Symbol.iterator]" || !(key in wrapped) || spread[key]() !== "called with inner") throw 3;
    delete inner[key];
    if (key in inner || !(key in spread)) throw 4;
    return true;
  })();
  export { result };`
);
async function checkIterator({ manifest, out, sourceFile, specFile }, check) {
  assertPublicEmissionRefused(manifest, out);
  assert.deepEqual(observe(sourceFile, "result"), { result: true });
  await withSplitPreview({ manifest }, ({ directory, entryFile }) => {
    assert.deepEqual(observe(entryFile, "result"), { result: true });
    const options = { source: sourceFile, spec: specFile, directory, observe: [["result"]] };
    const model = probeLinkedModel({ ...options, host });
    assert.equal(model.status, "matched", JSON.stringify(model));
    assert(model.fuel > 0);
    assert.deepEqual(model.observed, expectedModelObservations([[["result"], true]]));
    assert.deepEqual(model.assumptions, [premise]);
    check?.(options);
  });
}

await checkIterator(method, (options) => {
  const missing = probeLinkedModel(options);
  assert.equal(missing.status, "refused", JSON.stringify(missing));
  assert.match(missing.reason, /unsupported computed class key/);

  const owner = path.join(options.directory, "worker.mjs");
  const original = readFileSync(owner, "utf8");
  const changed = original.replace(/\[\s*Symbol\.iterator\s*\]/, '["Symbol.iterator"]');
  assert.notEqual(changed, original);
  writeFileSync(owner, changed);
  const mutated = probeLinkedModel({ ...options, host });
  assert.equal(mutated.status, "refused", JSON.stringify(mutated));
  assert.equal(mutated.reason_code, "linked_input", JSON.stringify(mutated));
  assert.match(mutated.reason, /emitted syntax differs from original unit 0/);
  assertPublicEmissionRefused(method.manifest, options.directory);
  assertPublicRecheckRefused(method.manifest, options.directory);
});

const shadow = fixture(
  "shadowed-symbol",
  `const Symbol = { iterator: "local" };
  const result = ({ [Symbol.iterator]: 42 }).local;
  export { result };`
);
assertPublicEmissionRefused(shadow.manifest, shadow.out);
await assertLinkedPreviewResult(shadow, 42);

const accessors = fixture(
  "iterator-accessors",
  `class Cursor {
    static get [Symbol.iterator]() { return this.saved; }
    static set [Symbol.iterator](value) { this.saved = value; }
  }
  const result = (() => {
    Cursor[Symbol.iterator] = 12;
    if (Cursor[Symbol.iterator] !== 12 || Cursor["Symbol.iterator"] !== undefined) throw 1;
    return true;
  })();
  export { result };`
);
await checkIterator(accessors);

const builtin = fixture(
  "native-array-iterator",
  `class Path { [Symbol.iterator]() { return [][Symbol.iterator](); } }
  const result = new Path()[Symbol.iterator]();
  export { result };`
);
assertPublicEmissionRefused(builtin.manifest, builtin.out);
await withSplitPreview({ manifest: builtin.manifest }, async ({ directory, entryFile }) => {
  for (const file of [builtin.sourceFile, entryFile]) {
    const { result } = await import(pathToFileURL(file).href);
    assert.deepEqual(result.next(), { value: undefined, done: true });
  }
  const model = probeLinkedModel({ source: builtin.sourceFile, spec: builtin.specFile, directory, host });
  assert.equal(model.status, "refused", JSON.stringify(model));
  assert.match(model.reason, /unknown/);
});
