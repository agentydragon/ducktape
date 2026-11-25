import assert from "node:assert/strict";
import path from "node:path";
import { assertCertified, lower, observe, runSplit } from "./harness.mjs";

const entry = "entry.mjs";

function checkSlice(test, scope, guarantee) {
  assertCertified(test.verdict, test.log);
  assert.equal(test.verdict.scope, scope, test.log);
  assert.equal(test.verdict.guarantee, guarantee, test.log);
  assert.equal(test.verdict.every_ending_matches, guarantee === "all-outcomes", test.log);
  assert.deepEqual(
    lower(test.files.sourceFile, test.files.specFile, test.out, ["--check-output"]).verdict,
    test.verdict
  );
}

const orderKept = runSplit("slice-order-kept", {
  source: "const left = []; const right = []; export { left, right };",
  provider: "",
  spec: { entry, modules: { [entry]: ["#0", "#1", "#2"] } },
});
checkSlice(orderKept, "linked-slice-order-kept", "all-outcomes");
assert.deepEqual(observe(path.join(orderKept.out, entry), "left"), { left: [] });

const moves = runSplit("slice-moves", {
  source: "const left = []; const right = []; export { left, right };",
  provider: "",
  spec: {
    entry,
    modules: { "right.mjs": ["#1"], [entry]: ["#0", "#2"] },
    order: ["right.mjs"],
    sequential: true,
  },
});
checkSlice(moves, "linked-slice-moves", "normal-completion");
assert.deepEqual(observe(path.join(moves.out, entry), "left"), { left: [] });

const bindings = runSplit("slice-certified-bindings", {
  source: "let x = 0; let y = 0; x = 1; y = 2; const done = x + y;",
  provider: "",
  spec: {
    entry,
    modules: { "m0.mjs": ["#1", "#3"], [entry]: ["#0", "#2", "#4"] },
  },
});
checkSlice(bindings, "linked-slice-certified-bindings", "normal-completion");

const heritage = runSplit("slice-heritage", {
  source: `
    var Base = class {};
    var Earlier = class extends Base {};
    var Pivot = class extends Base {};
    var config = { factor: 3 };
    var local = [5];
    var result = config.factor + local[0];
    export { result };
  `,
  provider: "",
  spec: {
    entry,
    modules: {
      "prefix.mjs": ["#0", "#1", "#3", "#4"],
      [entry]: ["#2", "#5", "#6"],
    },
  },
});
checkSlice(heritage, "linked-slice-heritage", "normal-completion");
assert.deepEqual(observe(path.join(heritage.out, entry), "result"), { result: 8 });

const providerFallback = runSplit("slice-provider-fallback", {
  source: `import { imported } from "./provider.mjs";
    const result = imported + 1;
    export { result };`,
  provider: "export const imported = 41;",
  spec: { entry, modules: { [entry]: ["#0", "#1", "#2"] } },
});
assertCertified(providerFallback.verdict, providerFallback.log);
assert.equal(providerFallback.verdict.scope, "linked-programs", providerFallback.log);
assert.equal(providerFallback.verdict.guarantee, "normal-completion", providerFallback.log);
assert.deepEqual(observe(providerFallback.files.sourceFile, "result"), { result: 42 });
assert.deepEqual(observe(path.join(providerFallback.out, entry), "result"), { result: 42 });
