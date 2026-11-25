import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, lower, root, threeUnitFixture as fixture } from "./harness.mjs";

const profile = path.join(root, "browser.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const options = ["--host-contract", profile];
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
assert.match(method.run().reason, /unsupported computed class key/);
const run = lower(method.sourceFile, method.specFile, method.out, options);
assertCertifiedResult({ ...run, files: method, out: method.out }, true);
assert.deepEqual(run.verdict.assumptions, run.verdict.scope === "linked-slice-order-kept" ? [] : [premise]);
assert.deepEqual(method.run(...options, "--check-output"), run.verdict);

const owner = path.join(method.out, "worker.mjs");
const original = readFileSync(owner, "utf8");
const changed = original.replace(/\[\s*Symbol\.iterator\s*\]/, '["Symbol.iterator"]');
assert.notEqual(changed, original);
writeFileSync(owner, changed);
assert.match(method.run(...options, "--check-output").reason, /worker\.mjs differs from its generated ESTree/);

const shadow = fixture(
  "shadowed-symbol",
  `const Symbol = { iterator: "local" };
  const result = ({ [Symbol.iterator]: 42 }).local;
  export { result };`
);
const shadowRun = lower(shadow.sourceFile, shadow.specFile, shadow.out, options);
assertCertifiedResult({ ...shadowRun, files: shadow, out: shadow.out }, 42);
assert.deepEqual(shadowRun.verdict.assumptions, []);

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
const accessorRun = lower(accessors.sourceFile, accessors.specFile, accessors.out, options);
assertCertifiedResult({ ...accessorRun, files: accessors, out: accessors.out }, true);
assert.deepEqual(
  accessorRun.verdict.assumptions,
  accessorRun.verdict.scope === "linked-slice-order-kept" ? [] : [premise]
);

const builtin = fixture(
  "native-array-iterator",
  `class Path { [Symbol.iterator]() { return [][Symbol.iterator](); } }
  const result = new Path()[Symbol.iterator]();
  export { result };`
);
const native = builtin.run(...options);
if (native.status === "certified") {
  assert.equal(native.scope, "linked-slice-order-kept", JSON.stringify(native));
  assert.equal(native.guarantee, "all-outcomes", JSON.stringify(native));
} else {
  assert.equal(native.status, "rejected");
  assert.match(native.reason, /unknown/);
}
