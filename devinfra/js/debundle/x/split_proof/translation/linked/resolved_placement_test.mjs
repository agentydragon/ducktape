import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { lower, moduleEvaluationOrder, prepare, root } from "./harness.mjs";

// The modules import each other. Creating read only captures value's live cell;
// the call to read observes the cell before value receives its initializer.
const source = `var read = () => typeof value;
  const before = read();
  var value = () => read();
  export { read, before, value };`;
const spec = {
  chunk_id: "app",
  source_sha256: createHash("sha256").update(source).digest("hex"),
  import_order: ["value", "reader"],
  units: ["reader", "reader", "value", null].map((owner, ordinal) => ({ owner, ordinal })),
};
const dir = path.join(root, "resolved-cycle");
const files = prepare(dir, { source, spec, provider: "" });
const out = path.join(dir, "out");
const opts = ["--structural"];
const run = (dest, flags, scope) => lower(files.sourceFile, files.specFile, dest, flags, scope).verdict;

// Visiting value first follows its request for reader. It does not evaluate value first.
const good = run(out, opts, "linked-structural-order");
assert.equal(good.status, "certified");
assert.deepEqual(run(out, [...opts, "--check-output"], "linked-structural-order"), good);
assert.deepEqual(moduleEvaluationOrder(out, "source.mjs"), ["reader.mjs", "value.mjs", "source.mjs"]);
for (const file of [files.sourceFile, path.join(out, "source.mjs")]) {
  const { before, read } = await import(pathToFileURL(file));
  assert.equal(before, "undefined");
  assert.equal(read(), "function");
}

function refuses(invalid, flags, scope, why) {
  writeFileSync(files.specFile, JSON.stringify(invalid));
  const dest = path.join(dir, "rejected");
  const verdict = run(dest, flags, scope);
  assert.equal(verdict.status, "rejected", JSON.stringify(verdict));
  assert.equal(verdict.guarantee, null);
  assert.match(verdict.reason, why);
  assert(!existsSync(dest));
}

// Reversing the SCC traversal moves the initializer ahead of the observation.
// The same evaluation order demanded explicitly may not be repaired silently.
const effect = /moved region|not a local allocating declaration/;
refuses({ ...spec, import_order: ["reader", "value"] }, opts, "linked-structural", effect);
refuses(
  {
    entry: "source.mjs",
    modules: { "source.mjs": ["#3"], "reader.mjs": ["#0", "#1"], "value.mjs": ["#2"] },
    order: ["value.mjs", "reader.mjs"],
    sequential: true,
  },
  ["--structural"],
  "linked-structural",
  effect
);
refuses({ ...spec, source_sha256: "0".repeat(64) }, opts, null, /source_sha256/);
refuses({ ...spec, units: spec.units.slice(1) }, opts, null, /unit count/);
refuses({ ...spec, sequential: true }, opts, null, /unexpected resolved placement field/);
