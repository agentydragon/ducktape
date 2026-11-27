import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { dump } from "./parser.mjs";
import { split } from "./testing/split.mjs";

const helper = `function collect(exit, closeThrows) {
  let trace = "";
  const iterable = {
    [Symbol.iterator]() {
      let seen = false;
      return {
        next() {
          trace += "next;";
          if (seen) return { done: true };
          seen = true;
          return { done: false, value: 7 };
        },
        return() {
          trace += "close;";
          if (closeThrows) throw "close error";
          return {};
        },
      };
    },
  };
  try {
    for (const value of iterable) {
      trace += value + ";";
      if (exit === "throw") throw "body error";
      if (exit === "break") break;
    }
  } catch (error) {
    trace += error + ";";
  }
  return trace;
}
`;
const entry = `export const early = collect("break", false);
export const closeError = collect("break", true);
export const bodyError = collect("throw", true);
export const exhausted = collect("exhaust", false);
`;
const source = helper + entry;
const expected = {
  early: "next;7;close;",
  closeError: "next;7;close;close error;",
  bodyError: "next;7;close;body error;",
  exhausted: "next;7;next;",
};
const names = Object.keys(expected);
const root = process.env.TEST_TMPDIR;
const sourceFile = path.join(root, "source.mjs");
const specFile = path.join(root, "placement.json");
const spec = {
  entry: "entry.mjs",
  modules: { "iterator.mjs": ["#0"], "entry.mjs": ["#1", "#2", "#3", "#4"] },
  order: ["iterator.mjs"],
  sequential: true,
};
writeFileSync(sourceFile, source);
writeFileSync(specFile, JSON.stringify(spec));
assert.deepEqual({ ...(await import(pathToFileURL(sourceFile))) }, expected);

// Exercise the requested cross-module call in Node independently of the splitter.
const candidate = path.join(root, "candidate");
mkdirSync(candidate);
writeFileSync(path.join(candidate, "iterator.mjs"), "export " + helper);
const candidateEntry = path.join(candidate, "entry.mjs");
writeFileSync(candidateEntry, 'import { collect } from "./iterator.mjs";\n' + entry);
assert.deepEqual({ ...(await import(pathToFileURL(candidateEntry))) }, expected);

// The recorded model runner exposes console output, so read the same exports
// with a final observation after initialization. Supply no opaque scripts.
const input = path.join(root, "model.jsonl");
const observedSource = source + names.map((name) => `console.log(${name});`).join("\n");
writeFileSync(input, JSON.stringify({ ...dump(observedSource), symbolConstructor: true, iteratorSymbol: true }) + "\n");
const modeled = JSON.parse(execFileSync(process.env.COREJS, ["--ast", input], { encoding: "utf8" }));
assert.deepEqual([modeled.end, modeled.out], ["normal", Object.values(expected)], JSON.stringify(modeled));

// Agreement on these executions does not prove that this split preserves the
// behavior. The ordinary CLI needs a constructor before emitting or reading it.
const request = { source: sourceFile, spec: specFile };
for (const require of ["all-outcomes", "normal-completion"]) {
  const emitted = split({ ...request, require, out: path.join(root, "output") });
  assert.equal(emitted.reasonCode, "no_constructed_rule", emitted.log);
  const checked = split({ ...request, require, out: candidate, checkOutput: true });
  assert.equal(checked.reasonCode, "no_constructed_rule", checked.log);
}
