import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { split } from "../testing/split.mjs";
import { observe, prepare, root } from "./harness.mjs";

const entry = "entry.mjs";
const spec = { entry, modules: { "worker.mjs": ["seed"], [entry]: ["result"] } };

function fixture(name, source) {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider: "", spec });
  return { ...files, out: path.join(dir, "out") };
}

function run(files, options = {}) {
  return split({ source: files.sourceFile, spec: files.specFile, out: files.out, require: "all-outcomes", ...options });
}

const sample = fixture("sparse", "const seed = 42;\nexport { seed as exposed };\nconst result = seed;\n");
const built = run(sample);
assert.equal(built.status, "certified", built.log);
assert.deepEqual(built.files, [entry, "worker.mjs"]);
const emittedEntry = path.join(sample.out, entry);
assert.deepEqual(observe(sample.sourceFile, "exposed"), { exposed: 42 });
assert.deepEqual(observe(emittedEntry, "exposed"), { exposed: 42 });
assert.equal(run(sample, { checkOutput: true }).status, "certified");

const originalEntry = readFileSync(emittedEntry, "utf8");
const wrongCell = originalEntry.replace(/export\s*\{\s*seed\s+as\s+exposed\s*\}/, "export { result as exposed }");
assert.notEqual(wrongCell, originalEntry);
writeFileSync(emittedEntry, wrongCell);
assert.deepEqual(observe(emittedEntry, "exposed"), { exposed: 42 });
const rejectedCell = run(sample, { checkOutput: true });
assert.equal(rejectedCell.status, "refused");
assert.match(rejectedCell.reason, /output module entry\.mjs differs/);

const wrongNode = originalEntry.replace(/const\s+result\s*=\s*seed/, "const result = 42");
assert.notEqual(wrongNode, originalEntry);
writeFileSync(emittedEntry, wrongNode);
const rejectedNode = run(sample, { checkOutput: true });
assert.equal(rejectedNode.status, "refused");
assert.match(rejectedNode.reason, /output module entry\.mjs differs/);
writeFileSync(emittedEntry, originalEntry);

const longer = fixture(
  "sparse-longer",
  "const seed = 9;\nexport { seed as exposed };\nexport {};\nexport {};\nconst result = seed;\n"
);
const longerBuilt = run(longer);
assert.equal(longerBuilt.status, "certified", longerBuilt.log);
assert.deepEqual(observe(path.join(longer.out, entry), "exposed"), { exposed: 9 });

const unsupported = fixture(
  "unsupported",
  "const seed = 42;\ndebugger;\nconst result = seed;\nexport { seed as exposed };\n"
);
const refused = run(unsupported);
assert.equal(refused.status, "refused");
assert.match(refused.reason, /unsupported omitted source node 1/);

const empty = fixture("empty-statement", "const seed = 42;\n;\nconst result = seed;\nexport { seed as exposed };\n");
const refusedEmpty = run(empty);
assert.equal(refusedEmpty.status, "refused");
assert.match(refusedEmpty.reason, /unsupported omitted source node 1/);
