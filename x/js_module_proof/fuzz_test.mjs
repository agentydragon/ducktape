import assert from "node:assert/strict";
import { writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";

if (process.env.ACORN_RLOCATION)
  process.env.JS_MODULE_PROOF_ACORN = resolve(process.env.TEST_SRCDIR, process.env.ACORN_RLOCATION);
process.env.JS_MODULE_PROOF_LEAN = resolve(process.env.TEST_SRCDIR, process.env.LEAN_RLOCATION);
if (process.env.TEST_SHARD_STATUS_FILE) await writeFile(process.env.TEST_SHARD_STATUS_FILE, "");
const { execute, makeCase, runFuzz } = await import("./fuzz.mjs");
const shard = Number(process.env.TEST_SHARD_INDEX ?? 0);
const shards = Number(process.env.TEST_TOTAL_SHARDS ?? 1);
assert.deepEqual(makeCase(123, 42), makeCase(123, 42));
assert.notDeepEqual(makeCase(123, 42), makeCase(124, 42));
const throwing = join(process.env.TEST_TMPDIR, `throwing-${shard}.mjs`);
await writeFile(throwing, "export function first() { return 42; } export function fail() { throw new Error(); }");
assert.deepEqual(execute(throwing, ["first", "fail"]), {
  events: [],
  exports: ["fail", "first"],
  results: [{ value: 42 }],
  error: { name: "Error", phase: "call", index: 1 },
});
const examples = Array.from({ length: 100 }, (_, i) => makeCase(123, i));
assert.ok(new Set(examples.map((c) => c.source)).size > 90);
assert.ok(new Set(examples.map((c) => JSON.stringify(c.spec))).size > 90);
const result = await runFuzz({ seed: 123, cases: 16, outputDir: process.env.TEST_TMPDIR, shard, shards });
assert.equal(result.cases, 16 / shards);
if (shards === 4 && shard === 0) assert.ok(result.proved > 0);
if (shards === 4 && shard === 1) assert.equal(result.unsafe, result.cases);
if (shards === 4 && shard === 3) assert.equal(result.refused, result.cases);
if (shards === 4 && shard === 2) {
  assert.ok(result.mutated > 0);
  assert.ok(result.different > 0);
}
assert.equal(result.generated, result.compared);
console.log(JSON.stringify(result));

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
assert.equal(bulk.generated, bulk.compared);
if (shards === 4 && shard === 1) assert.equal(bulk.different, bulk.cases);
if (shards === 4 && shard === 3) assert.equal(bulk.refused, bulk.cases);
console.log(JSON.stringify(bulk));
