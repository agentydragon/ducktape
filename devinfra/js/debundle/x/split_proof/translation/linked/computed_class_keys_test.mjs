import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  assertCertifiedResult,
  assertRejectedOrOrderKeptSlice,
  caseShard,
  lower,
  observe,
  prepare,
  root,
} from "./harness.mjs";

const runCase = caseShard();

function split(name, source, modules) {
  const files = prepare(path.join(root, name), {
    source,
    provider: "",
    spec: { entry: "entry.mjs", modules, order: ["base.mjs"], sequential: true },
  });
  const contract = path.join(root, name + "-browser.json");
  writeFileSync(contract, JSON.stringify({ profile: "browser-page" }));
  const options = ["--host-contract", contract];
  const out = path.join(root, name + "-output");
  return { ...lower(files.sourceFile, files.specFile, out, options), files, out, options };
}

if (runCase("order-receiver-names")) {
  const run = split(
    "computed-class-key-order-receiver-names",
    `const trace = { value: "" };
    class Base { read() { return this.value; } }
    let key = "run";
    const access = "amount";
    export class Child extends (trace.value += "h", Base) {
      [(trace.value += "m", key)]() { return super.read() + 1; }
      static { trace.value += "s"; }
      get [(trace.value += "g", access)]() { return this.value; }
      set [(trace.value += "w", access)](value) { this.value = value; }
      static [key]() { return this; }
    }
    key = "later";
    export const result = (() => {
      const child = new Child();
      child.amount = 5;
      return trace.value === "hmgws" && child.amount === 5 &&
        child.run() === 6 && child.run.name === "run" && Child.run() === Child && Child.run.name === "run" &&
        child.later === undefined;
    })();`,
    { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3", "#4", "#5", "#6"] }
  );
  assertCertifiedResult(run, true);
  const entry = path.join(run.out, "entry.mjs");
  for (const file of [run.files.sourceFile, entry]) {
    const { Child } = await import(pathToFileURL(file).href);
    const { get, set, enumerable } = Object.getOwnPropertyDescriptor(Child.prototype, "amount");
    assert.equal(get.name, "get amount");
    assert.equal(set.name, "set amount");
    assert.equal(enumerable, false);
  }
  const emitted = readFileSync(entry, "utf8");
  assert.match(emitted, /return super\.read\(\) \+ 1/);
  writeFileSync(entry, emitted.replace("return super.read() + 1", "return super.read() + 2"));
  assert.equal(
    lower(run.files.sourceFile, run.files.specFile, run.out, [...run.options, "--check-output"]).verdict.status,
    "rejected"
  );
  assert.deepEqual(observe(entry, "result"), { result: false });
}

if (runCase("async-iterator-symbol")) {
  const run = split(
    "computed-class-async-iterator-symbol",
    `const key = Symbol.asyncIterator;
    class Stream {
      static async [key]() { return 5; }
      [Symbol.asyncIterator]() { return this; }
    }
    export const result = (() => {
      const stream = new Stream();
      return stream[key]() === stream && stream[key].name === "[Symbol.asyncIterator]" &&
        Stream[key].name === "[Symbol.asyncIterator]";
    })();`,
    { "base.mjs": ["#0"], "entry.mjs": ["#1", "#2"] }
  );
  assertCertifiedResult(run, true);
  assert.deepEqual(
    run.verdict.assumptions,
    run.verdict.scope === "linked-slice-order-kept"
      ? []
      : [
          "Symbol.asyncIterator returns the well-known asyncIterator symbol without observable effects throughout initialization",
        ]
  );
  assert.deepEqual(
    lower(run.files.sourceFile, run.files.specFile, run.out, [...run.options, "--check-output"]).verdict,
    run.verdict
  );
}

for (const [name, sourceKey, memberKey, expectedName, reason] of [
  ["object-coercion", '{ toString() { return "read"; } }', '"read"', "read", /object is converted to a primitive/],
  ["symbol-description", 'Symbol("label")', "key", "[label]", /unmodeled symbol description/],
]) {
  if (!runCase(name)) continue;
  const refused = split(
    "computed-class-key-" + name,
    `const key = ${sourceKey};
    class Value { [key]() {} }
    export const result = Value.prototype[${memberKey}].name;`,
    { "base.mjs": ["#0"], "entry.mjs": ["#1", "#2"] }
  );
  assertRejectedOrOrderKeptSlice(refused, reason);
  assert.deepEqual(observe(refused.files.sourceFile, "result"), { result: expectedName });
}
