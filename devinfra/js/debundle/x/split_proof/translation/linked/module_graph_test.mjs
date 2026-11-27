import assert from "node:assert/strict";
import path from "node:path";
import { moduleGraphFixture } from "./module_graph_fixture.mjs";
import { assertCertified, lower, moduleEvaluationOrder, observe, prepare, root } from "./harness.mjs";

// Sixteen provider/reader pairs reach the staged linking threshold of 32 executable units.
const count = process.argv.includes("--full-graph") ? 48 : 16;
const { names, expected, source, entry, order, spec } = moduleGraphFixture(count);

const dir = path.join(root, "module-graph");
const files = prepare(dir, { source, provider: "", spec });
const out = path.join(dir, "out");
const { verdict, log } = lower(files.sourceFile, files.specFile, out, [], "indexed-local-exports");
assertCertified(verdict, log);
assert.equal(verdict.guarantee, "all-outcomes", log);
assert.equal(verdict.strategy, "compiled-constructor", log);
assert.deepEqual(
  lower(files.sourceFile, files.specFile, out, ["--check-output"], "indexed-local-exports").verdict,
  verdict
);
assert.deepEqual(moduleEvaluationOrder(out, entry), [...order, entry]);
for (const file of [files.sourceFile, path.join(out, entry)]) {
  assert.deepEqual(observe(file, names), expected);
}

// TODO: Support cyclic readers of mutable public aliases before testing that placement.
