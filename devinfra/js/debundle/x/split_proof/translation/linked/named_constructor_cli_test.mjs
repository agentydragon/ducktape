import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { split } from "../testing/split.mjs";
import { moduleEvaluationOrder } from "./harness.mjs";

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

// Logical initializers use the same authored #0/#1 placement and exact reread.
writeFileSync(spec, JSON.stringify(placement));
for (const [label, originalSource, expected] of [
  ["logical-and", "const seed = true; export const result = seed && 7;\n", 7],
  ["logical-or", "const seed = 0; export const result = seed || 7;\n", 7],
  ["logical-nullish", "const seed = null; export const result = seed ?? 7;\n", 7],
  ["logical-mixed", "const seed = 0; export const result = (seed ?? 1) || 7;\n", 7],
  ["logical-and-skipped", "const seed = false; export const result = seed && 7;\n", false],
  ["logical-or-skipped", "const seed = true; export const result = seed || 7;\n", true],
  ["logical-nullish-skipped", "const seed = 0; export const result = seed ?? 7;\n", 0],
  ["logical-skipped-read", "const seed = 7; export const result = false && seed;\n", false],
]) {
  writeFileSync(source, originalSource);
  const logicalOut = path.join(root, label);
  const logical = split({ source, spec, out: logicalOut, require: "all-outcomes" });
  assert.equal(logical.status, "certified", logical.log);
  assert.equal(logical.scope, "named-declarations", logical.log);
  assert.equal(logical.verdict.strategy, "compiled-constructor", logical.log);
  assert.equal((await import(pathToFileURL(path.join(logicalOut, "entry.mjs")))).result, expected);
  const checked = split({ source, spec, out: logicalOut, require: "all-outcomes", checkOutput: true });
  assert.equal(checked.status, "certified", checked.log);
  const entryFile = path.join(logicalOut, "entry.mjs");
  const original = readFileSync(entryFile, "utf8");
  if (label === "logical-skipped-read") assert.match(original, /import\s*\{\s*seed\s*\}/);
  const altered =
    label === "logical-skipped-read" ? original.replace(/\bseed(?=\s*;)/, "8") : original.replace(/7(?=\s*;)/, "8");
  assert.notEqual(altered, original);
  writeFileSync(entryFile, altered);
  const tampered = split({ source, spec, out: logicalOut, require: "all-outcomes", checkOutput: true });
  assert.equal(tampered.status, "refused", tampered.log);
  assert.equal(readFileSync(entryFile, "utf8"), altered);
}

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

// Preserve the original two-unit authored placement for primitive conditionals.
const conditionalPlacement = {
  entry: "entry.mjs",
  modules: { "part.mjs": ["#0"], "entry.mjs": ["#1"] },
  order: ["part.mjs"],
  sequential: true,
};
writeFileSync(spec, JSON.stringify(conditionalPlacement));
for (const [label, code, value] of [
  ["true", "const x=1; export const y=x===1?2:3;", 2],
  ["false", "const x=0; export const y=x===1?2:3;", 3],
  ["nested", "const x=1; export const y=x===1?(x?2:4):3;", 2],
]) {
  writeFileSync(source, code);
  const target = path.join(root, `conditional-${label}`);
  const result = split({ source, spec, out: target, require: "all-outcomes" });
  assert.equal(result.status, "certified", result.log);
  assert.equal(result.scope, "named-declarations", result.log);
  assert.equal(result.guarantee, "all-outcomes", result.log);
  assert.equal(result.verdict.strategy, "compiled-constructor", result.log);
  assert.deepEqual(result.files, ["entry.mjs", "part.mjs"]);
  assert.match(readFileSync(path.join(target, "part.mjs"), "utf8"), /const x\s*=/);
  assert.match(readFileSync(path.join(target, "entry.mjs"), "utf8"), /const y\s*=/);
  assert.deepEqual(moduleEvaluationOrder(target, "entry.mjs"), ["part.mjs", "entry.mjs"]);
  assert.equal((await import(pathToFileURL(source).href + `?${label}`)).y, value);
  assert.equal((await import(pathToFileURL(path.join(target, "entry.mjs")).href)).y, value);
  assert.equal(split({ source, spec, out: target, require: "all-outcomes", checkOutput: true }).status, "certified");
  if (label === "true") {
    const entryFile = path.join(target, "entry.mjs");
    const emitted = readFileSync(entryFile, "utf8");
    const altered = emitted.replace(/\?\s*2\s*:\s*3/, "? 3 : 3");
    assert.notEqual(altered, emitted);
    writeFileSync(entryFile, altered);
    assert.equal(split({ source, spec, out: target, require: "all-outcomes", checkOutput: true }).status, "refused");
    assert.equal(readFileSync(entryFile, "utf8"), altered);
  }
}

// A skipped branch is still checked against the earlier-binding policy.
writeFileSync(source, "const x=1; export const y=x===1?2:missing;");
const unboundConditionalOut = path.join(root, "conditional-unbound");
assert.equal(split({ source, spec, out: unboundConditionalOut, require: "all-outcomes" }).status, "refused");
assert(!existsSync(unboundConditionalOut));

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
assert.equal(uninitialized.verdict.strategy, "compiled-constructor");
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

// The inline constructor must account for every original node, including a
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

// The authored selectors must resolve and cover every executable unit exactly
// once, even when distinct selector spellings claim the same unit.
writeFileSync(source, "const seed = 7; export const result = seed;\n");
for (const [label, modules, reason] of [
  [
    "unknown-ordinal",
    { "worker.mjs": ["#0", "#99"], "entry.mjs": ["#1"] },
    /source position 99 has no executable unit/,
  ],
  ["unknown-binding", { "worker.mjs": ["#0", "unknown"], "entry.mjs": ["#1"] }, /source binding unknown is missing/],
  ["missing-unit", { "worker.mjs": [], "entry.mjs": ["#1"] }, /selectors do not cover every original unit/],
  [
    "alias-duplicate",
    { "worker.mjs": ["#0", "seed"], "entry.mjs": ["#1"] },
    /an original unit is selected more than once/,
  ],
]) {
  writeFileSync(spec, JSON.stringify({ ...placement, modules }));
  const refused = run({ out: path.join(root, label) });
  assert.equal(refused.status, "refused", refused.log);
  assert.equal(refused.scope, "named-declarations", refused.log);
  assert.match(refused.reason, reason);
}

for (const [label, request] of [
  ["wrong-order", { ...placement, order: [] }],
  ["non-sequential", { ...placement, sequential: false }],
  ["moved-inline-export", { ...placement, modules: { "worker.mjs": ["#0", "#1"], "entry.mjs": [] } }],
]) {
  writeFileSync(spec, JSON.stringify(request));
  const refused = run({ out: path.join(root, label) });
  assert.equal(refused.status, "refused", refused.log);
}

// Admission is checked against real lexical source, including reads nested
// inside primitive expressions. The parser rejects duplicate declarations.
for (const [label, originalSource] of [
  ["duplicate-declaration", "const seed = 1; export const seed = 2;"],
  ["forward-read", "const seed = result; export const result = 7;"],
  ["nested-forward-read", "const seed = 1 + -result; export const result = 7;"],
  ["self-read", "const seed = seed; export const result = seed;"],
  ["lexical-forward-read", "let seed = result; export let result = 7;"],
]) {
  writeFileSync(source, originalSource);
  writeFileSync(spec, JSON.stringify(placement));
  const refused = run({ out: path.join(root, label) });
  assert.equal(refused.status, "refused", refused.log);
}

// Linked lowering can model a computed optional method call, but that alone
// does not make its placement eligible for the ordinary constructor.
writeFileSync(spec, JSON.stringify(placement));
writeFileSync(source, 'const seed = null; export const result = seed?.["value"]();');
for (const guarantee of ["normal-completion", "all-outcomes"]) {
  const optionalOut = path.join(root, `optional-computed-${guarantee}`);
  const optional = split({ source, spec, out: optionalOut, require: guarantee });
  assert.equal(optional.status, "refused", optional.log);
  assert(!existsSync(optionalOut));
}

// These placements used to publish via the linked evaluator. A supported
// placement shape alone cannot justify moving the declaration.
writeFileSync(spec, JSON.stringify(placement));
for (const declaration of [
  "const seed = Math.random();",
  "class seed { static value = 1; }",
  "async function seed() { return 1; }",
  "function* seed() { yield 1; }",
]) {
  writeFileSync(source, declaration + "\nexport const result = seed;\n");
  const refusedOut = path.join(root, "refused");
  const refused = split({ source, spec, out: refusedOut });
  assert.equal(refused.status, "refused", refused.log);
  assert.equal(refused.reasonCode, "no_constructed_rule", refused.log);
  assert(!existsSync(refusedOut));
}
