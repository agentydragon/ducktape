import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { split } from "../testing/split.mjs";

const root = process.env.TEST_TMPDIR;
const source = path.join(root, "source.mjs");
const spec = path.join(root, "spec.json");
const out = path.join(root, "out");
const placement = {
  entry: "entry.mjs",
  modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1"] },
  order: ["worker.mjs"],
  sequential: true,
};
writeFileSync(source, "const seed = 7; export const result = seed;\n");
writeFileSync(spec, JSON.stringify(placement));
const run = (extra = {}) => split({ source, spec, out, require: "all-outcomes", ...extra });
const good = run();
assert.equal(good.status, "certified", good.log);
assert.equal(good.scope, "named-declarations");
assert.equal(good.guarantee, "all-outcomes");
assert.equal(good.verdict.strategy, "compiled-constructor");
assert.deepEqual(good.files, ["entry.mjs", "worker.mjs"]);
assert.match(readFileSync(path.join(out, "worker.mjs"), "utf8"), /\bconst seed = 7;/);
assert.match(readFileSync(path.join(out, "entry.mjs"), "utf8"), /\bconst result = seed;/);
const emitted = await import(pathToFileURL(path.join(out, "entry.mjs")));
assert.equal(emitted.result, 7);
assert.equal(run({ checkOutput: true }).status, "certified");

// Initialized lexical declarations use the same actual-source constructor.
writeFileSync(source, "let seed = 7; export let result = seed;\n");
const letOut = path.join(root, "let-out");
const letResult = split({ source, spec, out: letOut, require: "all-outcomes" });
assert.equal(letResult.status, "certified", letResult.log);
assert.equal(letResult.scope, "named-declarations");
assert.equal(letResult.guarantee, "all-outcomes");
assert.deepEqual(letResult.files, ["entry.mjs", "worker.mjs"]);
assert.match(readFileSync(path.join(letOut, "worker.mjs"), "utf8"), /\blet seed = 7;/);
assert.match(readFileSync(path.join(letOut, "entry.mjs"), "utf8"), /\blet result = seed;/);
assert.equal((await import(pathToFileURL(path.join(letOut, "entry.mjs")))).result, 7);
assert.equal(split({ source, spec, out: letOut, require: "all-outcomes", checkOutput: true }).status, "certified");

const entry = path.join(out, "entry.mjs");
const original = readFileSync(entry, "utf8");
const changed = original.replace(/result = seed/, "result = 7");
assert.notEqual(changed, original);
writeFileSync(entry, changed);
assert.equal(run({ checkOutput: true }).status, "refused");
writeFileSync(entry, original);

// A duplicated source selector must not change an existing output tree.
const before = good.files.map((file) => readFileSync(path.join(out, file), "utf8"));
writeFileSync(spec, JSON.stringify({ ...placement, modules: { "worker.mjs": ["#0"], "entry.mjs": ["#0", "#1"] } }));
assert.equal(run({ allowExistingOut: true }).status, "refused");
assert.deepEqual(
  good.files.map((file) => readFileSync(path.join(out, file), "utf8")),
  before
);

// A forward lexical read keeps its temporal-dead-zone failure. Reversing these
// units must not take the all-outcomes constructor path or publish output.
writeFileSync(source, "let first = later; let later = 7; export { first };\n");
writeFileSync(
  spec,
  JSON.stringify({
    entry: "entry.mjs",
    modules: { "worker.mjs": ["#1"], "entry.mjs": ["#0", "#2"] },
    order: ["worker.mjs"],
    sequential: true,
  })
);
const tdzOut = path.join(root, "tdz-refused");
const tdz = split({ source, spec, out: tdzOut, require: "all-outcomes" });
assert.equal(tdz.status, "refused", tdz.log);
assert(!existsSync(tdzOut));

// Unsupported initializers must not publish anything on this path.
writeFileSync(source, "const seed = Math.random(); export const result = seed;\n");
writeFileSync(spec, JSON.stringify(placement));
const refusedOut = path.join(root, "refused");
const refused = split({ source, spec, out: refusedOut, require: "all-outcomes" });
assert.equal(refused.status, "refused", refused.log);
assert(!existsSync(refusedOut));
