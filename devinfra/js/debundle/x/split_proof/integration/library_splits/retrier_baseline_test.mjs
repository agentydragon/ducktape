import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync } from "node:fs";
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
  release();
  const [left, right] = await results;
  assert.deepEqual(left, { status: "rejected", reason: error });
  assert.equal(left.reason, error);
  assert.deepEqual(right, { status: "fulfilled", value: 7 });
  const drained = [retrier.working, retrier.pending, retrier.retrying];
  assert.deepEqual(drained, [0, 0, 0]);
  assert.deepEqual(trace, ["first:start", "first:write", "check:failed", "second:start", "second:write"]);
  return { trace, queued, drained, error: left.reason.message, value: right.value };
}

const spec = fileURLToPath(new URL("./retrier/split.json", import.meta.url));
const originalSpec = readFileSync(spec);
const root = process.env.TEST_TMPDIR;
const dest = path.join(root, "certified");
const scratch = path.join(root, "scratch-estree-only");
const request = { source, spec, out: dest, require: "all-outcomes" };
const emit = split(request);
assert.deepEqual(
  emit.verdict,
  {
    every_ending_matches: false,
    guarantee: null,
    reason: "no constructor accepts the requested placement of entry.mjs",
    reason_code: "no_constructed_rule",
    requested: "all-outcomes",
    scope: null,
    status: "rejected",
  },
  emit.log
);
assert.equal(existsSync(dest), false);

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
const recheck = split({ ...request, out: scratch, checkOutput: true });
assert.deepEqual(recheck.verdict, emit.verdict);

const Original = await loadPublic(source);
const Staged = await loadPublic(path.join(scratch, "entry.mjs"));
const original = { success: await consume(Original), discriminator: await distinguishOrderAndError(Original) };
const staged = { success: await consume(Staged), discriminator: await distinguishOrderAndError(Staged) };
assert.deepEqual(staged, original);
assert.deepEqual(readFileSync(source), originalBytes);
assert.deepEqual(readFileSync(spec), originalSpec);
console.log(JSON.stringify({ emit: emit.verdict, recheck: recheck.verdict, diagnostic, original, staged }));
