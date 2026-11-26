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

const omitted = runSplit("omitted-export-unit", {
  source: "const result = [].length; export { result };",
  provider: "",
  spec: { entry, modules: { [entry]: ["#0", "#1"] } },
});
assertCertified(omitted.verdict, omitted.log);
assert.equal(omitted.verdict.guarantee, "normal-completion", omitted.log);
assert.equal(omitted.verdict.scope, "linked-programs", omitted.log);
assert.deepEqual(
  lower(omitted.files.sourceFile, omitted.files.specFile, omitted.out, ["--check-output"]).verdict,
  omitted.verdict
);
const omittedOut = path.join(omitted.dir, "all-outcomes");
assertRejected(
  { out: omittedOut, ...lower(omitted.files.sourceFile, omitted.files.specFile, omittedOut, allOutcomes) },
  /proves only normal-completion/
);
const omittedDisk = lower(omitted.files.sourceFile, omitted.files.specFile, omitted.out, [
  ...allOutcomes,
  "--check-output",
]);
assert.equal(omittedDisk.verdict.status, "rejected", omittedDisk.log);
assert.match(omittedDisk.verdict.reason, /proves only normal-completion/, omittedDisk.log);

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
const normal = lower(profiled.sourceFile, profiled.specFile, profiledOut, host);
assertCertified(normal.verdict, normal.log);
assert.equal(normal.verdict.scope, "linked-programs", normal.log);
assert.equal(normal.verdict.guarantee, "normal-completion", normal.log);
assert.deepEqual(normal.verdict.assumptions, [
  "Symbol.iterator returns the well-known iterator symbol without observable effects throughout initialization",
]);
assert.deepEqual(observe(profiled.sourceFile, "result"), { result: 42 });
assert.deepEqual(observe(path.join(profiledOut, entry), "result"), { result: 42 });
assert.deepEqual(
  lower(profiled.sourceFile, profiled.specFile, profiledOut, [...host, "--check-output"]).verdict,
  normal.verdict
);
const profiledAll = path.join(profiledDir, "all-outcomes");
assertRejected(
  { out: profiledAll, ...lower(profiled.sourceFile, profiled.specFile, profiledAll, [...host, ...allOutcomes]) },
  /proves only normal-completion/
);
const profiledDisk = lower(profiled.sourceFile, profiled.specFile, profiledOut, [
  ...host,
  ...allOutcomes,
  "--check-output",
]);
assert.equal(profiledDisk.verdict.status, "rejected", profiledDisk.log);
assert.match(profiledDisk.verdict.reason, /proves only normal-completion/, profiledDisk.log);

const profiledWorker = path.join(profiledOut, "worker.mjs");
const workerText = readFileSync(profiledWorker, "utf8");
const changedWorker = workerText.replace(/\[\s*Symbol\.iterator\s*\]/, '["Symbol.iterator"]');
assert.notEqual(changedWorker, workerText);
writeFileSync(profiledWorker, changedWorker);
for (const request of [[], allOutcomes]) {
  const tampered = lower(profiled.sourceFile, profiled.specFile, profiledOut, [...host, ...request, "--check-output"]);
  assert.equal(tampered.verdict.status, "rejected", tampered.log);
  assert.match(tampered.verdict.reason, /worker\.mjs differs from its generated ESTree/, tampered.log);
  assert(existsSync(profiledWorker));
}
