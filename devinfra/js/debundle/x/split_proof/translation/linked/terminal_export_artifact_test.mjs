import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { split } from "../testing/split.mjs";
import { observe, prepare, root } from "./harness.mjs";

const entry = "entry.mjs";
const worker = "provider.mjs";
const spec = { entry, modules: { [worker]: ["#0"], [entry]: ["#1"] }, order: [worker], sequential: true };
const files = prepare(path.join(root, "terminal-export"), {
  source: "const a = 1;\nexport { a };\n",
  provider: "",
  spec,
});
const out = path.join(root, "terminal-export-out");
const run = (options = {}) =>
  split({ source: files.sourceFile, spec: files.specFile, out, require: "all-outcomes", ...options });
const original = run();
assert.equal(original.status, "certified", original.log);
assert.equal(original.guarantee, "all-outcomes");
assert.deepEqual(original.files, [entry, worker]);
assert.deepEqual(observe(files.sourceFile, "a"), { a: 1 });
assert.deepEqual(observe(path.join(out, entry), "a"), { a: 1 });
assert.equal(run({ checkOutput: true }).status, "certified");

const entryFile = path.join(out, entry);
const workerFile = path.join(out, worker);
const entryBytes = readFileSync(entryFile);
const workerBytes = readFileSync(workerFile);
writeFileSync(entryFile, "const decoy = 1; export { decoy as a };\n");
const wrongCellBytes = readFileSync(entryFile);
const badCell = run({ checkOutput: true, allowExistingOut: true });
assert.equal(badCell.status, "refused", badCell.log);
assert.deepEqual(readFileSync(entryFile), wrongCellBytes);
assert.deepEqual(readFileSync(workerFile), workerBytes);
writeFileSync(entryFile, entryBytes);

for (const [name, badSpec] of [
  ["wrong-owner", { ...spec, modules: { [worker]: ["#0", "#1"], [entry]: [] } }],
  ["wrong-ordinal", { ...spec, modules: { [worker]: ["#0"], [entry]: ["#2"] } }],
  ["duplicate-ordinal", { ...spec, modules: { [worker]: ["#0"], [entry]: ["#1", "#1"] } }],
  ["missing-export", { ...spec, modules: { [worker]: ["#0"], [entry]: [] } }],
]) {
  const badSpecFile = path.join(root, name + ".json");
  writeFileSync(badSpecFile, JSON.stringify(badSpec));
  const refused = run({ spec: badSpecFile, allowExistingOut: true });
  assert.equal(refused.status, "refused", refused.log);
  assert.doesNotMatch(refused.reason, /terminal-export-not-applicable/, refused.log);
  assert.deepEqual(readFileSync(entryFile), entryBytes);
  assert.deepEqual(readFileSync(workerFile), workerBytes);
  assert.deepEqual(readdirSync(out).sort(), [entry, worker].sort());
}

const fresh = path.join(root, "wrong-ordinal-fresh");
const badSpecFile = path.join(root, "wrong-ordinal.json");
assert.equal(run({ spec: badSpecFile, out: fresh }).status, "refused");
assert(!existsSync(fresh));

// A different valid source belongs to another strategy, not a refusal by this one.
const other = prepare(path.join(root, "other-source"), {
  source: "const a = 1;\nexport const b = 2;\n",
  provider: "",
  spec,
});
const otherOut = path.join(root, "other-source-out");
const otherResult = split({ source: other.sourceFile, spec: other.specFile, out: otherOut, require: "all-outcomes" });
assert.equal(otherResult.status, "refused", otherResult.log);
assert.match(otherResult.reason, /terminal-export-not-applicable/, otherResult.log);
assert(!existsSync(otherOut));
