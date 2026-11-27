import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
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

// The same compiled constructor admits logical negation of an earlier binding.
writeFileSync(source, "const seed = 7; const ready = !seed; export const result = ready;\n");
const negationPlacement = {
  entry: "entry.mjs",
  modules: { "worker.mjs": ["#0", "#1"], "entry.mjs": ["#2"] },
  order: ["worker.mjs"],
  sequential: true,
};
writeFileSync(spec, JSON.stringify(negationPlacement));
const negationOut = path.join(root, "negation-out");
const negation = split({ source, spec, out: negationOut, require: "all-outcomes" });
assert.equal(negation.status, "certified", negation.log);
assert.equal(negation.scope, "named-declarations", JSON.stringify(negation));
assert.equal(negation.guarantee, "all-outcomes");
assert.equal(negation.verdict.strategy, "compiled-constructor");
assert.match(readFileSync(path.join(negationOut, "worker.mjs"), "utf8"), /const ready = !seed;/);
assert.equal((await import(pathToFileURL(path.join(negationOut, "entry.mjs")))).result, false);
assert.equal(split({ source, spec, out: negationOut, require: "all-outcomes", checkOutput: true }).status, "certified");

// Earlier-binding binary expressions in initialized lexical declarations use the compiled proof.
writeFileSync(source, "let seed = 7; let ready = seed + 2; export let result = ready;\n");
const arithmeticLetPlacement = {
  entry: "entry.mjs",
  modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
  order: ["worker.mjs"],
  sequential: true,
};
writeFileSync(spec, JSON.stringify(arithmeticLetPlacement));
const arithmeticLetOut = path.join(root, "arithmetic-let-out");
const arithmeticLet = split({ source, spec, out: arithmeticLetOut, require: "all-outcomes" });
assert.equal(arithmeticLet.status, "certified", arithmeticLet.log);
assert.equal(arithmeticLet.scope, "named-declarations", JSON.stringify(arithmeticLet));
assert.equal(arithmeticLet.guarantee, "all-outcomes");
assert.equal(arithmeticLet.verdict.strategy, "compiled-constructor");
assert.match(readFileSync(path.join(arithmeticLetOut, "entry.mjs"), "utf8"), /let ready = seed \+ 2;/);
const arithmeticLetEntry = await import(pathToFileURL(path.join(arithmeticLetOut, "entry.mjs")));
assert.equal(arithmeticLetEntry.result, 9);
assert.equal(
  split({ source, spec, out: arithmeticLetOut, require: "all-outcomes", checkOutput: true }).status,
  "certified"
);

// Initialized lexical declarations use the same actual-source constructor.
writeFileSync(spec, JSON.stringify(placement));
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
const letEntry = path.join(letOut, "entry.mjs");
const letOriginal = readFileSync(letEntry, "utf8");
const letChanged = letOriginal.replace(/result = seed/, "result = 7");
assert.notEqual(letChanged, letOriginal);
writeFileSync(letEntry, letChanged);
assert.equal(split({ source, spec, out: letOut, require: "all-outcomes", checkOutput: true }).status, "refused");
assert.equal(readFileSync(letEntry, "utf8"), letChanged);
writeFileSync(letEntry, letOriginal);

// An uninitialized lexical declaration keeps its original live cell.
writeFileSync(source, "let seed; export let result = seed;\n");
const uninitializedOut = path.join(root, "uninitialized-let-out");
const uninitialized = split({ source, spec, out: uninitializedOut, require: "all-outcomes" });
assert.equal(uninitialized.status, "certified", uninitialized.log);
assert.equal(uninitialized.scope, "named-declarations");
assert.equal((await import(pathToFileURL(path.join(uninitializedOut, "entry.mjs")))).result, undefined);
assert.equal(
  split({ source, spec, out: uninitializedOut, require: "all-outcomes", checkOutput: true }).status,
  "certified"
);

// Exercise tampering against the same source and placement that produced `out`.
writeFileSync(source, "const seed = 7; export const result = seed;\n");
assert.equal(run({ checkOutput: true }).status, "certified");
const entry = path.join(out, "entry.mjs");
const original = readFileSync(entry, "utf8");
const changed = original.replace(/result = seed/, "result = 7");
assert.notEqual(changed, original);
writeFileSync(entry, changed);
assert.equal(run({ checkOutput: true }).status, "refused");
assert.equal(readFileSync(entry, "utf8"), changed);
writeFileSync(entry, original);

// A duplicated source selector must not change an existing output tree.
const before = good.files.map((file) => readFileSync(path.join(out, file), "utf8"));
writeFileSync(spec, JSON.stringify({ ...placement, modules: { "worker.mjs": ["#0"], "entry.mjs": ["#0", "#1"] } }));
assert.equal(run({ allowExistingOut: true }).status, "refused");
assert.deepEqual(
  good.files.map((file) => readFileSync(path.join(out, file), "utf8")),
  before
);

// The dense constructor must account for every original node, including a
// standalone export row and a debugger omitted by the CoreJS decoder.
writeFileSync(spec, JSON.stringify(placement));
for (const [label, originalSource] of [
  ["unclaimed-export", "const seed = 7; const result = seed; export { result };\n"],
  ["omitted-debugger", "const seed = 7; export const result = seed; debugger;\n"],
]) {
  writeFileSync(source, originalSource);
  for (const guarantee of ["normal-completion", "all-outcomes"]) {
    const target = path.join(root, `${label}-${guarantee}`);
    const fresh = split({ source, spec, out: target, require: guarantee });
    assert.equal(fresh.status, "refused", fresh.log);
    if (label === "omitted-debugger") {
      assert.equal(fresh.scope, "named-declarations", fresh.log);
      assert.match(fresh.reason, /source provenance: unsupported omitted source node 2/);
    }
    assert(!existsSync(target));
    mkdirSync(target);
    const sentinel = path.join(target, "sentinel.txt");
    writeFileSync(sentinel, "preserve existing bytes\n");
    const existing = split({ source, spec, out: target, require: guarantee, allowExistingOut: true });
    assert.equal(existing.status, "refused", existing.log);
    if (label === "omitted-debugger") {
      assert.equal(existing.scope, "named-declarations", existing.log);
      assert.match(existing.reason, /source provenance: unsupported omitted source node 2/);
    }
    assert.equal(readFileSync(sentinel, "utf8"), "preserve existing bytes\n");
  }
}

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
