import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { createRequire } from "node:module";

if (process.env.ACORN_RLOCATION)
  process.env.JS_MODULE_PROOF_ACORN = resolve(process.env.TEST_SRCDIR, process.env.ACORN_RLOCATION);
process.env.JS_MODULE_PROOF_LEAN = resolve(process.env.TEST_SRCDIR, process.env.LEAN_RLOCATION);
if (process.env.TEST_SHARD_STATUS_FILE) await writeFile(process.env.TEST_SHARD_STATUS_FILE, "");
const { execute, makeCase, minimizeCase, runFuzz } = await import("./fuzz.mjs");
const shard = Number(process.env.TEST_SHARD_INDEX ?? 0);
const shards = Number(process.env.TEST_TOTAL_SHARDS ?? 1);
assert.deepEqual(makeCase(123, 42), makeCase(123, 42));
assert.notDeepEqual(makeCase(123, 42), makeCase(124, 42));
const shrinkInput = makeCase(123, 0);
const keptNode = JSON.stringify(shrinkInput.ast.body[0]);
const keptCall = shrinkInput.calls[0];
const shrunk = await minimizeCase(
  shrinkInput,
  async (candidate) =>
    candidate.ast.body.some((node) => JSON.stringify(node) === keptNode) && candidate.calls.includes(keptCall)
);
assert.equal(shrunk.ast.body.filter((node) => node.type !== "ExportNamedDeclaration").length, 1);
assert.deepEqual(shrunk.calls, [keptCall]);
assert.equal(Object.values(shrunk.spec.modules).flatMap((module) => module.statements).length, 1);
assert.deepEqual(
  Object.values(shrunk.spec.modules).flatMap((module) => module.statements),
  [0]
);
assert.ok(Object.keys(shrunk.spec.modules).length <= 2);
const throwing = join(process.env.TEST_TMPDIR, `throwing-${shard}.mjs`);
await writeFile(throwing, "export function first() { return 42; } export function fail() { throw new Error(); }");
assert.deepEqual(execute(throwing, ["first", "fail"]), {
  events: [],
  exports: ["fail", "first"],
  results: [{ value: 42 }],
  error: { name: "Error", phase: "call", index: 1, thrown: { kind: "opaque", name: "Error" } },
});
const primitiveThrow = join(process.env.TEST_TMPDIR, `primitive-throw-${shard}.mjs`);
await writeFile(primitiveThrow, "export function first() { return 42; } export function fail() { throw 'changed'; }");
assert.deepEqual(execute(primitiveThrow, ["first", "fail"]), {
  events: [],
  exports: ["fail", "first"],
  results: [{ value: 42 }],
  error: { name: null, phase: "call", index: 1, thrown: { kind: "string", value: "changed" } },
});
const nullThrow = join(process.env.TEST_TMPDIR, `null-throw-${shard}.mjs`);
await writeFile(nullThrow, "export function fail() { throw null; }");
assert.deepEqual(execute(nullThrow, ["fail"]).error, {
  name: null,
  phase: "call",
  index: 0,
  thrown: { kind: "null" },
});
const opaqueThrow = join(process.env.TEST_TMPDIR, `opaque-throw-${shard}.mjs`);
await writeFile(opaqueThrow, "export function fail() { throw { value: 1 }; }");
assert.equal(execute(opaqueThrow, ["fail"]).error.thrown.kind, "opaque");
const timeout = join(process.env.TEST_TMPDIR, `timeout-${shard}.mjs`);
await writeFile(timeout, "while (true) {}");
assert.deepEqual(execute(timeout, [], { timeoutMs: 250 }), { inconclusive: "timeout" });
const result = await runFuzz({ seed: 123, cases: 16, outputDir: process.env.TEST_TMPDIR, shard, shards });
console.log(JSON.stringify(result));
assert.equal(result.cases, 16 / shards);
if (shards === 4 && shard === 0) {
  assert.ok(result.proved > 0);
  assert.ok(result.constantCases > 0);
}
if (shards === 4 && shard === 1) assert.equal(result.unsafe, result.cases);
if (shards === 4 && shard === 3) {
  assert.equal(result.closureCases + result.modeledClosureCases, result.cases);
  assert.equal(result.modeledClosureCases, 1);
  assert.equal(result.modeledClosureCompared, 1);
  assert.equal(result.modeledClosureProved, 1);
  assert.equal(result.modeledClosureThrows, 1);
  assert.equal(result.closureSourceExecuted, result.closureCases);
  assert.equal(result.closureRefused, result.closureCases);
  const unsafeClosure = await runFuzz({ seed: 123, start: 19, cases: 1, outputDir: process.env.TEST_TMPDIR });
  assert.equal(unsafeClosure.modeledClosureCompared, 1);
  assert.equal(unsafeClosure.modeledClosureUnsafe, 1);
}
if (shards === 4 && shard === 2) {
  assert.ok(result.mutated > 0);
  assert.ok(result.different > 0);
}
assert.equal(result.generated, result.compared + result.inconclusive);

// Exercise many additional programs against Node without paying for a Lean
// invocation on every case. Validation is sampled independently by index.
const bulk = await runFuzz({
  seed: 456,
  cases: 128,
  outputDir: process.env.TEST_TMPDIR,
  shard,
  shards,
  validateEvery: 1000000000,
});
assert.equal(bulk.cases, 128 / shards);
assert.equal(bulk.generated, bulk.compared + bulk.inconclusive);
if (shards === 4 && shard === 1) assert.equal(bulk.different, bulk.cases);
if (shards === 4 && shard === 3) {
  assert.equal(bulk.closureSourceExecuted, bulk.closureCases);
  assert.equal(bulk.modeledClosureCases, 8);
  assert.equal(bulk.modeledClosureCompared, 8);
  assert.ok(bulk.modeledClosureCrossModule > 0);
  assert.equal(bulk.closureRefused, bulk.closureCases);
  assert.equal(bulk.controlFlowCases, bulk.cases / 2);
  assert.ok(bulk.branchThen > 0);
  assert.ok(bulk.branchElse > 0);
}
console.log(JSON.stringify(bulk));

// Run structural sampling after the Lean-backed cases to limit peak memory.
const parse = createRequire(import.meta.url)(process.env.JS_MODULE_PROOF_ACORN ?? "acorn").parse;
const closures = Array.from({ length: 128 }, (_, i) => makeCase(917, 4 * i + 3)).filter((c) => c.grammar === "closure");
assert.deepEqual(makeCase(917, 15), makeCase(917, 15));
assert.equal(new Set(closures.map((c) => c.depth)).size, 4);
assert.ok(closures.some((c) => c.shadowed));
assert.ok(closures.some((c) => c.writesCaptured));
assert.ok(closures.some((c) => !c.writesCaptured));
assert.equal(closures.filter((c) => c.controlFlow).length, 64);
function countNodes(node, type) {
  if (!node || typeof node !== "object") return 0;
  return (
    (node.type === type ? 1 : 0) +
    Object.values(node).reduce(
      (sum, child) =>
        sum +
        (Array.isArray(child) ? child.reduce((n, item) => n + countNodes(item, type), 0) : countNodes(child, type)),
      0
    )
  );
}
for (const c of closures) {
  assert.equal(countNodes(c.ast, "IfStatement"), c.controlFlow ? 1 : 0);
  assert.equal(countNodes(c.ast, "ForStatement"), c.controlFlow ? 1 : 0);
}
assert.ok(new Set(closures.map((c) => c.source)).size > 80);
for (const c of closures) {
  const parsed = parse(c.source, { ecmaVersion: "latest", sourceType: "module" });
  assert.deepEqual(
    parsed.body.map((n) => n.type),
    c.ast.body.map((n) => n.type)
  );
}
const escaping = closures.find((c) => c.depth === 1 && c.writesCaptured && !c.controlFlow);
assert.ok(escaping);
const closurePath = join(process.env.TEST_TMPDIR, `closure-${shard}.mjs`);
await writeFile(closurePath, escaping.source);
const observedClosure = execute(closurePath, escaping.calls);
assert.equal(observedClosure.error, null);
assert.ok(new Set(observedClosure.results.map((item) => item.value)).size > 1);
assert.deepEqual(execute(closurePath, escaping.calls), observedClosure);
const flow = closures.find((c) => c.controlFlow);
const flowPath = join(process.env.TEST_TMPDIR, `flow-${shard}.mjs`);
await writeFile(flowPath, flow.source);
const observedFlow = execute(flowPath, flow.calls);
assert.equal(observedFlow.error, null);
assert.equal(observedFlow.events.filter((n) => n === 101 || n === 102).length, flow.calls.length);
assert.equal(observedFlow.events.filter((n) => n === 0).length, flow.calls.length);
assert.deepEqual(execute(flowPath, flow.calls), observedFlow);
const examples = Array.from({ length: 100 }, (_, i) => makeCase(123, i));
assert.ok(new Set(examples.map((c) => c.source)).size > 90);
assert.ok(new Set(examples.map((c) => JSON.stringify(c.spec))).size > 90);
