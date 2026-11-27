import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertified, assertRejected, lower, observe, prepare, root, runSplit } from "./harness.mjs";

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
  source: "const left = []; const other = [99]; export const result = left.length;",
  provider: "",
  spec: { entry, modules: { "left.mjs": ["#0"], [entry]: ["#1", "#2"] }, order: ["left.mjs"] },
});
checkSlice(orderKept, "linked-slice-order-kept", "all-outcomes");
assert.deepEqual(observe(orderKept.files.sourceFile, "result"), { result: 0 });
assert.deepEqual(observe(path.join(orderKept.out, entry), "result"), { result: 0 });
const allOutcomes = ["--require", "all-outcomes"];
const exact = lower(orderKept.files.sourceFile, orderKept.files.specFile, orderKept.out, [
  ...allOutcomes,
  "--check-output",
]);
assertCertified(exact.verdict, exact.log);
assert.equal(exact.verdict.scope, "linked-slice-order-kept", exact.log);
assert.equal(exact.verdict.guarantee, "all-outcomes", exact.log);
assert.equal(exact.verdict.requested, "all-outcomes", exact.log);
const emitExact = path.join(orderKept.dir, "all-outcomes");
assert.deepEqual(
  lower(orderKept.files.sourceFile, orderKept.files.specFile, emitExact, allOutcomes).verdict,
  exact.verdict
);

const changedEntry = path.join(emitExact, entry);
const entryText = readFileSync(changedEntry, "utf8");
const wrongBinding = entryText.replace("left.length", "other.length");
assert.notEqual(wrongBinding, entryText);
writeFileSync(changedEntry, wrongBinding);
for (const request of [[], allOutcomes]) {
  const changed = lower(orderKept.files.sourceFile, orderKept.files.specFile, emitExact, [
    ...request,
    "--check-output",
  ]);
  assert.equal(changed.verdict.status, "rejected", changed.log);
  assert.match(changed.verdict.reason, /entry\.mjs differs from its generated ESTree/, changed.log);
}

const moves = runSplit("slice-moves", {
  source: "export const left = []; const right = [];",
  provider: "",
  spec: {
    entry,
    modules: { "right.mjs": ["#1"], [entry]: ["#0"] },
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
    export var result = config.factor + local[0];
  `,
  provider: "",
  spec: {
    entry,
    modules: {
      "prefix.mjs": ["#0", "#1", "#3", "#4"],
      [entry]: ["#2", "#5"],
    },
  },
});
checkSlice(heritage, "linked-slice-heritage", "normal-completion");
assert.deepEqual(observe(path.join(heritage.out, entry), "result"), { result: 8 });

const providerSource = prepare(path.join(root, "provider-source"), {
  source: `import { imported } from "./provider.mjs";
    const result = imported + 1;
    export { result };`,
  provider: "export const imported = 41;",
  spec: { entry, modules: { [entry]: ["#0", "#1", "#2"] } },
});
assert.deepEqual(observe(providerSource.sourceFile, "result"), { result: 42 });

const profile = path.join(root, "browser.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const host = ["--host-contract", profile];
const profiledDir = path.join(root, "profile-dependent-slice");
const profiled = prepare(profiledDir, {
  source: `class Collection { [Symbol.iterator]() { return 42; } }
    export const result = new Collection()[Symbol.iterator]();`,
  provider: "",
  spec: { entry, modules: { "worker.mjs": ["#0"], [entry]: ["#1"] }, order: ["worker.mjs"] },
});
const profiledOut = path.join(profiledDir, "out");
assertRejected(
  { out: profiledOut, ...lower(profiled.sourceFile, profiled.specFile, profiledOut) },
  /unsupported computed class key/
);
assert.deepEqual(observe(profiled.sourceFile, "result"), { result: 42 });
for (const request of [[], allOutcomes]) {
  const unsupported = lower(profiled.sourceFile, profiled.specFile, profiledOut, [...host, ...request]);
  assert.equal(unsupported.verdict.reason_code, "slice_profile", unsupported.log);
  assert(!existsSync(profiledOut));
}
