import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, lower, root, threeUnitFixture as fixture } from "./harness.mjs";

const profile = path.join(root, "browser.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const options = ["--host-contract", profile];

const tag = fixture(
  "class-string-tag",
  `class Box {
    get [Symbol.toStringTag]() { return this.label; }
    [Symbol.iterator]() { return 17; }
  }
  const result = (() => {
    const box = new Box();
    box.label = "Box";
    const key = Symbol.toStringTag;
    if (typeof key !== "symbol" || key !== Symbol.toStringTag || key === Symbol.iterator) throw 1;
    if (box[key] !== "Box" || box[Symbol.iterator]() !== 17 || box["Symbol.toStringTag"] !== undefined) throw 2;
    return true;
  })();
  export { result };`
);
assert.match(tag.run().reason, /unsupported computed class key/);
const run = lower(tag.sourceFile, tag.specFile, tag.out, options);
assertCertifiedResult({ ...run, files: tag, out: tag.out }, true);
if (run.verdict.scope === "linked-slice-order-kept") assert.deepEqual(run.verdict.assumptions, []);
else {
  assert.ok(
    run.verdict.assumptions.includes(
      "Symbol.toStringTag returns the well-known toStringTag symbol without observable effects throughout initialization"
    )
  );
}
assert.deepEqual(tag.run(...options, "--check-output"), run.verdict);
const file = path.join(tag.out, "worker.mjs");
const source = readFileSync(file, "utf8");
const changed = source.replace(/\[\s*Symbol\.toStringTag\s*\]/, '["Symbol.toStringTag"]');
assert.notEqual(source, changed);
writeFileSync(file, changed);
assert.match(tag.run(...options, "--check-output").reason, /worker\.mjs differs from its generated ESTree/);
