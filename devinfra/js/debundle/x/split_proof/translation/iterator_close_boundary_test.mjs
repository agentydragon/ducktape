import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
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

// A fixed array parameter runs before the body. It reads the selected
// values, skips the elided value getter, and closes the custom iterator.
const pick = `function pick([first, , third], suffix) {
  var first;
  return first + ":" + third + ":" + suffix + ":" + arguments.length;
}
`;
const consumers = `function runParameter(mode) {
  let trace = "";
  let step = 0;
  const iterable = {
    [Symbol.iterator]() {
      trace += "acquire;";
      return {
        next() {
          trace += "next;";
          if (mode === "next throws") throw "next error";
          step += 1;
          if (mode === "exhaust" && step === 2) return { done: true };
          const value = step;
          return {
            done: false,
            get value() { trace += "value;"; return value; },
          };
        },
        return() {
          trace += "close;";
          if (mode === "close throws") throw "close error";
          return {};
        },
      };
    },
  };
  try {
    const result = pick(iterable, "tag");
    return trace + result;
  } catch (error) {
    return trace + error;
  }
}
export const parameterClosed = runParameter("close");
export const parameterCloseError = runParameter("close throws");
export const parameterNextError = runParameter("next throws");
export const parameterExhausted = runParameter("exhaust");
export const parameterArity = pick.length;
`;
const parameterSource = pick + consumers;
const parameterExpected = {
  parameterClosed: "acquire;next;value;next;next;value;close;1:3:tag:2",
  parameterCloseError: "acquire;next;value;next;next;value;close;close error",
  parameterNextError: "acquire;next;next error",
  parameterExhausted: "acquire;next;value;next;1:undefined:tag:2",
  parameterArity: 2,
};
const parameterFile = path.join(root, "parameter.mjs");
writeFileSync(parameterFile, parameterSource);
assert.deepEqual({ ...(await import(pathToFileURL(parameterFile))) }, parameterExpected);

const parameterOutput = path.join(root, "parameter-split");
mkdirSync(parameterOutput);
writeFileSync(path.join(parameterOutput, "iterator.mjs"), "export " + pick);
const parameterEntry = path.join(parameterOutput, "entry.mjs");
writeFileSync(parameterEntry, 'import { pick } from "./iterator.mjs";\n' + consumers);
assert.deepEqual({ ...(await import(pathToFileURL(parameterEntry))) }, parameterExpected);

const observeParameters = Object.keys(parameterExpected)
  .map((name) => `console.log(${name});`)
  .join("\n");
for (const [entrySource, modules] of [
  [parameterSource, {}],
  [
    readFileSync(parameterEntry, "utf8"),
    {
      entry: "entry.mjs",
      providers: { "iterator.mjs": dump(readFileSync(path.join(parameterOutput, "iterator.mjs"), "utf8")) },
    },
  ],
]) {
  writeFileSync(
    input,
    JSON.stringify({
      ...dump(entrySource + observeParameters),
      ...modules,
      symbolConstructor: true,
      iteratorSymbol: true,
    }) + "\n"
  );
  const parameterModeled = JSON.parse(execFileSync(process.env.COREJS, ["--ast", input], { encoding: "utf8" }));
  assert.deepEqual(
    [parameterModeled.end, parameterModeled.out],
    ["normal", Object.values(parameterExpected).map(String)],
    JSON.stringify(parameterModeled)
  );
}

const parameterSpec = path.join(root, "parameter-placement.json");
writeFileSync(
  parameterSpec,
  JSON.stringify({
    ...spec,
    modules: { "iterator.mjs": ["#0"], "entry.mjs": ["#1", "#2", "#3", "#4", "#5", "#6"] },
  })
);
const parameterRequest = { source: parameterFile, spec: parameterSpec, require: "all-outcomes" };
const parameterEmission = split({ ...parameterRequest, out: path.join(root, "parameter-certified") });
assert.equal(parameterEmission.reasonCode, "no_constructed_rule", parameterEmission.log);
const parameterReadback = split({ ...parameterRequest, out: parameterOutput, checkOutput: true });
assert.equal(parameterReadback.reasonCode, "no_constructed_rule", parameterReadback.log);

// Ordinary lookup of the computed iterator key can throw before pick starts.
// Compare both actual module layouts without using the recorded host model.
const throwingSymbol = `
const original = Object.getOwnPropertyDescriptor(globalThis, "Symbol");
let reads = 0;
Object.defineProperty(globalThis, "Symbol", {
  configurable: true,
  get() { reads += 1; throw "ambient Symbol getter"; },
});
let thrown;
try { await import(process.argv[1]); } catch (error) { thrown = error; }
Object.defineProperty(globalThis, "Symbol", original);
console.log(JSON.stringify({ reads, thrown }));
`;
for (const file of [parameterFile, parameterEntry]) {
  const result = execFileSync(
    process.execPath,
    ["--input-type=module", "-e", throwingSymbol, pathToFileURL(file).href],
    {
      encoding: "utf8",
    }
  );
  assert.deepEqual(JSON.parse(result), { reads: 1, thrown: "ambient Symbol getter" });
}
