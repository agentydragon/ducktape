import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { acorn } from "../../translation/parser.mjs";

const input = path.resolve(process.argv[2], "dist/retrier.js");
const inputBytes = readFileSync(input);
assert.equal(
  createHash("sha256").update(inputBytes).digest("hex"),
  "86a1ccc63d7cce261886d404d4e3aa38dd40bbf802d9c3e9340ca047e5e86e66"
);
const text = inputBytes.toString("utf8");
const root = process.env.TEST_TMPDIR;
const original = acorn.parse(text, { ecmaVersion: "latest", sourceType: "module" }).body[7];
assert.equal(original.id.name, "RetryTask");
const exactClass = text.slice(original.start, original.end);
const project = (name, cls) => {
  const file = path.join(root, name + ".mjs");
  writeFileSync(file, cls + "\nexport { RetryTask };\n");
  return file;
};
const projection = project("projected", exactClass);
const dir = path.join(root, "staged");
const run = (source, stage, mode = "stage") => {
  const out = spawnSync(process.env.RETRYTASK_CLASS, [input, source, stage, mode], { encoding: "utf8" });
  assert.ifError(out.error);
  return out;
};
const accepted = run(projection, dir);
assert.equal(accepted.status, 0, accepted.stdout + accepted.stderr);
const worker = path.join(dir, "task.mjs");
const workerBytes = readFileSync(worker, "utf8");
assert(workerBytes.includes(exactClass), "class source must survive the move byte-for-byte");

const draw = "Math.random().toString(36).slice(2)";
const altered = (from, to) => {
  const mutant = exactClass.replace(from, to);
  assert.notEqual(mutant, exactClass);
  return mutant;
};
const eager = altered("id = " + draw, "static id = " + draw);
for (const [name, cls, reason] of [
  ["eager", eager, /static class member/],
  ["heritage", altered("class RetryTask", "class RetryTask extends (() => Object)()"), /class heritage is eager/],
  ["key", altered("id = " + draw, '["id"] = ' + draw), /computed class key is eager/],
]) {
  const result = run(project(name, cls), path.join(root, name + "-stage"), "control");
  assert.notEqual(result.status, 0, result.stdout);
  assert.match(result.stderr, reason);
}

// An identically spelled Date reference now closes over an added lexical cell.
const changedCapture = "const Date = { now: () => 99 };\n" + workerBytes;
writeFileSync(worker, changedCapture);
const wrongCapture = run(projection, dir, "check");
assert.notEqual(wrongCapture.status, 0, wrongCapture.stdout);
assert.match(wrongCapture.stderr, /differs|readback refused/);
assert.equal(readFileSync(worker, "utf8"), changedCapture);
writeFileSync(worker, workerBytes);

// Moving the draw is rejected against the authentic class even before staging.
const movedDraw = altered("id = " + draw, "id = 0").replace("this.fn = fn;", "this.fn = fn; this.id = " + draw + ";");
const wrongTiming = run(project("moved-draw", movedDraw), path.join(root, "moved-draw"));
assert.notEqual(wrongTiming.status, 0, wrongTiming.stdout);
assert.match(wrongTiming.stderr, /projected class bytes differ from ordinal 7/);
const changedTiming = workerBytes.replace(exactClass, movedDraw);
writeFileSync(worker, changedTiming);
const wrongReread = run(projection, dir, "check");
assert.notEqual(wrongReread.status, 0, wrongReread.stdout);
assert.match(wrongReread.stderr, /differs|readback refused/);
assert.equal(readFileSync(worker, "utf8"), changedTiming);
writeFileSync(worker, workerBytes);

// Node is a discriminator only. No stand-in is supplied to the Lean checker.
async function trace(url) {
  const events = [];
  const originalMath = globalThis.Math;
  const originalDate = globalThis.Date;
  const draws = [0.5, 0.75];
  let cursor = 0;
  const math = {
    get random() {
      events.push("random lookup");
      return function () {
        assert.equal(this, math);
        events.push("draw");
        return draws[cursor++];
      };
    },
  };
  globalThis.Math = math;
  globalThis.Date = { now: () => 23 };
  try {
    const cls = (await import(pathToFileURL(url).href)).RetryTask;
    const creation = events.slice();
    const first = new cls(null, null, null, null);
    const afterFirst = events.slice();
    const second = new cls(null, null, null, null);
    return {
      creation,
      afterFirst,
      events,
      ids: [first.id, second.id],
      timestamps: [first.timestamp, second.timestamp],
    };
  } finally {
    globalThis.Math = originalMath;
    globalThis.Date = originalDate;
  }
}
const expected = {
  creation: [],
  afterFirst: ["random lookup", "draw"],
  events: ["random lookup", "draw", "random lookup", "draw"],
  ids: ["i", "r"],
  timestamps: [23, 23],
};
assert.deepEqual(await trace(projection), expected);
assert.deepEqual(await trace(path.join(dir, "entry.mjs")), expected);
assert.deepEqual((await trace(path.join(root, "eager.mjs"))).creation, ["random lookup", "draw"]);
assert.deepEqual(readFileSync(input), inputBytes);
