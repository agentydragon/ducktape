import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { split } from "../../translation/testing/split.mjs";

const packageRoot = process.argv[2];
const { name, version, type, exports } = JSON.parse(readFileSync(path.join(packageRoot, "package.json"), "utf8"));
assert.deepEqual(
  [name, version, type, exports.import.default],
  ["@humanwhocodes/retry", "0.4.3", "module", "./dist/retrier.js"]
);
const source = path.join(packageRoot, exports.import.default);
const originalBytes = readFileSync(source);
assert.equal(
  createHash("sha256").update(originalBytes).digest("hex"),
  "86a1ccc63d7cce261886d404d4e3aa38dd40bbf802d9c3e9340ca047e5e86e66"
);

async function loadPublic(entry) {
  const mod = await import(pathToFileURL(path.resolve(entry)).href);
  assert.deepEqual(Object.keys(mod), ["Retrier"]);
  return mod.Retrier;
}

async function consume(Retrier) {
  const retrier = new Retrier(() => assert.fail("success should not call the retry predicate"));
  const value = await retrier.retry(async () => 42);
  assert.equal(value, 42);
  return value;
}

async function distinguishOrderAndError(Retrier) {
  const trace = [];
  const error = new Error("task failed");
  const retrier = new Retrier(
    (reason) => {
      assert.equal(reason, error);
      trace.push("check:failed");
      return false;
    },
    { concurrency: 1 }
  );
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  const first = retrier.retry(async () => {
    trace.push("first:start");
    await held;
    trace.push("first:write");
    throw error;
  });
  const second = retrier.retry(async () => {
    trace.push("second:start", "second:write");
    return 7;
  });
  const results = Promise.allSettled([first, second]);
  const queued = [retrier.working, retrier.pending, retrier.retrying];
  assert.deepEqual(trace, ["first:start"]);
  assert.deepEqual(queued, [1, 1, 0]);
  const queueTrace = [];
  const shift = Array.prototype.shift;
  let pair;
  try {
    Array.prototype.shift = function () {
      // During release, the Retrier is idle with exactly one pending job.
      // Forward the actual method with the receiver it was given.
      const observe =
        retrier.working === 0 && retrier.pending === 1 && this.length === 1 && typeof this[0] === "function";
      const first = observe ? this[0] : undefined;
      const task = Reflect.apply(shift, this, []);
      if (observe) queueTrace.push([task === first, retrier.pending, retrier.working]);
      return task;
    };
    release();
    pair = await results;
  } finally {
    Array.prototype.shift = shift;
  }
  const [left, right] = pair;
  assert.deepEqual(left, { status: "rejected", reason: error });
  assert.equal(left.reason, error);
  assert.deepEqual(right, { status: "fulfilled", value: 7 });
  const drained = [retrier.working, retrier.pending, retrier.retrying];
  assert.deepEqual(drained, [0, 0, 0]);
  assert.deepEqual(trace, ["first:start", "first:write", "check:failed", "second:start", "second:write"]);
  assert.deepEqual(queueTrace, [[true, 0, 0]]);
  return { trace, queueTrace, queued, drained, error: left.reason.message, value: right.value };
}

const spec = fileURLToPath(new URL("./retrier/split.json", import.meta.url));
const originalSpec = readFileSync(spec);
const root = process.env.TEST_TMPDIR;
const dest = path.join(root, "certified");
const scratch = path.join(root, "scratch-estree-only");
const request = { source, spec, out: dest, require: "all-outcomes" };
const emit = split(request);
assert.equal(emit.status, "certified", emit.log);
assert.equal(emit.scope, "retained-classes", emit.log);
assert.equal(emit.guarantee, "all-outcomes", emit.log);
assert.deepEqual(emit.files, ["entry.mjs", "retry-task.mjs"]);
assert.deepEqual(emit.verdict.receipt.graph.public_entry.exports, ["Retrier"]);
assert.deepEqual(emit.verdict.receipt.graph.output.evaluation_order, ["retry-task.mjs", "entry.mjs"]);

const diagnostic = JSON.parse(
  execFileSync(process.env.RETRIER_BASELINE, [source, spec, scratch], { encoding: "utf8" })
);
assert.deepEqual(readdirSync(scratch).sort(), ["entry.mjs", "retry-task.mjs"]);
assert.deepEqual(diagnostic, {
  order: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
  classification: "statement 7: unsupported global Math",
  indexed: "statement 7: unsupported global Math",
  linked: "statement 7: unsupported global Math",
  directUnitRefusals: [
    [3, "unsupported global globalThis"],
    [4, "unsupported global Date"],
    [6, "unsupported global Promise"],
    [7, "unsupported global Math"],
    [8, "unsupported global Error"],
  ],
});
const recheck = split({ ...request, checkOutput: true });
assert.equal(recheck.status, "certified", recheck.log);
assert.equal(recheck.guarantee, "all-outcomes", recheck.log);
assert.deepEqual(recheck.verdict.receipt, { ...emit.verdict.receipt, operation: "checked-output" });

const Original = await loadPublic(source);
const Published = await loadPublic(path.join(dest, "entry.mjs"));
const original = { success: await consume(Original), discriminator: await distinguishOrderAndError(Original) };
const published = { success: await consume(Published), discriminator: await distinguishOrderAndError(Published) };
assert.deepEqual(published, original);

const publicEntry = path.join(dest, "entry.mjs");
const entryBytes = readFileSync(publicEntry, "utf8");
const addedExport = entryBytes + "\nexport { Retrier as unexpected };\n";
writeFileSync(publicEntry, addedExport);
const tampered = split({ ...request, checkOutput: true });
assert.equal(tampered.status, "refused", tampered.log);
assert.match(tampered.reason, /differs from its generated ESTree/);
assert.equal(readFileSync(publicEntry, "utf8"), addedExport);
writeFileSync(publicEntry, entryBytes);

const duplicateSpec = path.join(root, "duplicate-source-id.json");
const duplicate = JSON.parse(originalSpec);
duplicate.modules["entry.mjs"].push("#7");
writeFileSync(duplicateSpec, JSON.stringify(duplicate));
const refused = split({ ...request, spec: duplicateSpec, out: path.join(root, "duplicate") });
assert.equal(refused.status, "refused", refused.log);
assert.deepEqual(refused.files, []);
assert.deepEqual(readFileSync(source), originalBytes);
assert.deepEqual(readFileSync(spec), originalSpec);
console.log(JSON.stringify({ emit: emit.verdict, recheck: recheck.verdict, diagnostic, original, published }));
