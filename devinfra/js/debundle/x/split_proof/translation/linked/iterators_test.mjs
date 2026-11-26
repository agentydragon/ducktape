import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import path from "node:path";
import {
  assertCertified,
  assertRejectedOrOrderKeptSlice,
  caseShard,
  lower,
  observe,
  prepare,
  root,
  sequentialSplit,
} from "./harness.mjs";

const entry = "entry.mjs";
const runCase = caseShard();

if (runCase("native-array-and-fresh-cells")) {
  const source = `
    const [first, , { n: second = first + 1 }, ...rest] = [2, 0, {}, 4];
    function run() {
      let firstRead, secondRead, count = 0;
      for (let [value] of [[first], [second]]) {
        if (count === 0) firstRead = () => value;
        else secondRead = () => value;
        count += 1;
      }
      let last;
      for (last of [5]) continue;
      return [firstRead(), secondRead(), rest[0], last];
    }
    const result = run();
    export { result };
  `;
  const { files, out, verdict, log } = sequentialSplit("native-array-and-fresh-cells", source, {
    "worker.mjs": ["#0", "#1"],
    [entry]: ["#2", "#3"],
  });
  assertCertified(verdict, log);
  assert.deepEqual(lower(files.sourceFile, files.specFile, out, ["--check-output"]).verdict, verdict);
  for (const file of [files.sourceFile, path.join(out, entry)])
    assert.deepEqual(observe(file, "result"), { result: [2, 3, 4, 5] });
}

const profile = path.join(root, "browser.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));

function browserCase(name, source, expected) {
  if (!runCase(name)) return;
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source,
    provider: "",
    spec: {
      entry,
      modules: { "worker.mjs": ["#0", "#1", "#2"], [entry]: ["#3", "#4"] },
      order: ["worker.mjs"],
      sequential: true,
    },
  });
  const out = path.join(dir, "out");
  const verdict = lower(files.sourceFile, files.specFile, out, ["--host-contract", profile]);
  assertCertified(verdict.verdict, verdict.log);
  for (const file of [files.sourceFile, path.join(out, entry)])
    assert.deepEqual(observe(file, "result"), { result: expected });
}

browserCase(
  "custom-iterator",
  `
  let trace = "";
  class Iterable {
    [Symbol.iterator]() {
      trace += "I";
      const cursor = {
        index: 0,
        get next() {
          trace += "N";
          return function advance() {
            if (this !== cursor) throw 1;
            this.index += 1;
            return {
              get done() { trace += "D"; return false; },
              get value() { trace += "V"; return cursor.index * 10; },
            };
          };
        },
        return() {
          if (this !== cursor) throw 2;
          trace += "R";
          return {};
        },
      };
      return cursor;
    }
  }
  function run() {
    const [, value] = new Iterable();
    let first = 0;
    const array = [100];
    array[Symbol.iterator] = new Iterable()[Symbol.iterator];
    for (const next of array) { first = next; break; }
    return [value, first, trace];
  }
  const result = run();
  export { result };
  `,
  [20, 10, "INDDVRINDVR"]
);

browserCase(
  "iterator-abrupt-completion",
  `
  let closed = 0;
  class Iterable {
    constructor(failStep) { this.failStep = failStep; }
    [Symbol.iterator]() {
      const fail = this.failStep;
      return {
        next() {
          if (fail) throw 3;
          return { done: false, value: 1 };
        },
        return() { closed += 1; throw 2; },
      };
    }
  }
  function run() {
    let fromBody, fromClose, fromStep;
    try { for (const value of new Iterable(false)) throw value; } catch (error) { fromBody = error; }
    try { for (const value of new Iterable(false)) break; } catch (error) { fromClose = error; }
    try { const [value] = new Iterable(true); } catch (error) { fromStep = error; }
    return [fromBody, fromClose, fromStep, closed];
  }
  const result = run();
  export { result };
  `,
  [1, 2, 3, 2]
);

if (runCase("loop-head-tdz")) {
  const source = `
    const values = [1];
    for (const values of values) {}
    export { values };
  `;
  const run = sequentialSplit("loop-head-tdz", source, { [entry]: ["#0", "#1", "#2"] }, []);
  assertRejectedOrOrderKeptSlice(run, /original threw during linked initialization/);
  assert.deepEqual(observe(run.files.sourceFile, "values"), { error: "ReferenceError" });
}
