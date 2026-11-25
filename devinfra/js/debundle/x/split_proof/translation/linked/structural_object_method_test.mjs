import assert from "node:assert/strict";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { forcePreview, observe, root, structuralSplit } from "./harness.mjs";

const contract = path.join(root, "portable.json");
writeFileSync(contract, JSON.stringify({ profile: "ecmascript", stableIntrinsicValues: ["Object.defineProperty@1"] }));
const enabled = ["--host-contract", contract];
const pivot = "linked-structural-pivot",
  refusal = "linked-structural";

function fixture(name, before, init, after = "export {saved};") {
  return structuralSplit(name, `${before}; var saved = ${init}; ${after}`, {
    "entry.mjs": after.startsWith("export") ? ["#0", "#2"] : ["#0", "#2", "#3"],
    "owner.mjs": ["#1"],
  });
}
const read = fixture("read", "var marker = 0", "Object.defineProperty");
assert.equal(read.run(refusal).status, "rejected");
const certified = read.run(pivot, ...enabled);
assert.equal(certified.status, "certified");
assert.equal(certified.hostProfile, "ecmascript");
assert.match(
  certified.assumptions.join("\n"),
  /Object\.defineProperty@1:.*ambient binding Object returns intrinsic\.Object.*stable host reference intrinsic\.Object\.defineProperty; both reads have no observable effects/
);
assert.deepEqual(read.run(pivot, ...enabled, "--check-output"), certified);
assert.equal(read.run(refusal, "--check-output").status, "rejected");
for (const file of [read.sourceFile, path.join(read.out, "entry.mjs")]) {
  assert.equal((await import(pathToFileURL(file))).saved, Object.defineProperty);
}
for (const [name, before, expr] of [
  ["invoke", "var marker = 0", 'Object.defineProperty({}, "x", {value: 1})'],
  ["coerce", "var marker = 0", "Object.defineProperty / 2"],
  ["other-key", "var marker = 0", "Object.getOwnPropertyNames"],
  ["shadow", "export const Object = {get defineProperty() {return 1}}", "Object.defineProperty"],
]) {
  const test = fixture(name, before, expr);
  assert.equal(test.run(refusal, ...enabled).status, "rejected");
  assert(!existsSync(test.out));
}
// Each observation runs in a fresh Node process. Replacing the ambient method
// violates the descriptor: default certification must not authorize the move.
const replaced = fixture(
  "replaced",
  "Object.defineProperty = () => 1",
  "Object.defineProperty",
  "const same = saved === Object.defineProperty; export {same};"
);
assert.equal(replaced.run(refusal).status, "rejected");
const preview = path.join(replaced.dir, "preview");
forcePreview(replaced, preview);
assert.deepEqual(observe(replaced.sourceFile, "same"), { same: true });
assert.deepEqual(observe(path.join(preview, "entry.mjs"), "same"), { same: false });
