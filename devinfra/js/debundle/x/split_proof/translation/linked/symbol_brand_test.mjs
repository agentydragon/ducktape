import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, lower, root, threeUnitFixture as fixture } from "./harness.mjs";

const profile = path.join(root, "browser.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const options = ["--host-contract", profile];

const brand = fixture(
  "symbol-brand",
  `const brand = Symbol("RecordBrand");
  const result = (() => {
    let key = brand;
    const Box = class { [key] = true; };
    key = Symbol("RecordBrand");
    const a = new Box(), b = new Box();
    if (typeof brand !== "symbol" || key === brand || Symbol() === Symbol()) throw "freshness";
    if (a[brand] !== true || b[brand] !== true || key in a || "RecordBrand" in a) throw "field key";
    if (brand in Box.prototype || brand in Box) throw "field owner";
    delete a[brand];
    if (brand in a || b[brand] !== true) throw "per-instance descriptor";
    return true;
  })();
  export { result };`
);
const run = lower(brand.sourceFile, brand.specFile, brand.out, options);
assertCertifiedResult({ ...run, files: brand, out: brand.out }, true);
assert.deepEqual(
  run.verdict.assumptions,
  run.verdict.scope === "linked-slice-order-kept"
    ? []
    : ["Symbol returns the intrinsic Symbol function without observable effects throughout initialization"]
);
assert.deepEqual(brand.run(...options, "--check-output"), run.verdict);

const owner = path.join(brand.out, "worker.mjs");
const original = readFileSync(owner, "utf8");
const changed = original.replace(/Symbol\(["']RecordBrand["']\)/, '"RecordBrand"');
assert.notEqual(changed, original);
writeFileSync(owner, changed);
assert.match(brand.run(...options, "--check-output").reason, /worker\.mjs differs from its generated ESTree/);

const shadow = fixture(
  "shadowed-symbol",
  `const Symbol = name => name;
  const result = (() => {
    const key = Symbol("local");
    const Box = class { [key] = 42; };
    return new Box().local;
  })();
  export { result };`
);
const shadowRun = lower(shadow.sourceFile, shadow.specFile, shadow.out, options);
assertCertifiedResult({ ...shadowRun, files: shadow, out: shadow.out }, 42);
assert.deepEqual(shadowRun.verdict.assumptions, []);

const namedField = fixture(
  "computed-field-function-name",
  `const brand = Symbol("callback");
  class Box { [brand] = () => 1; }
  export { Box };`
);
assert.match(namedField.run(...options).reason, /unsupported function or class named by a computed field key/);
