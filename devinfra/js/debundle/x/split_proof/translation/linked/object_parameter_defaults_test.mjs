import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, assertRejected, caseShard, lower, observe, sequentialSplit } from "./harness.mjs";

const includesCase = caseShard();

if (includesCase("derived-getter-and-default-order")) {
  const test = sequentialSplit(
    "derived-getter-and-default-order",
    `const fallback = "missing";
    const trace = { value: "" };
    const mark = (step, value) => { trace.value += step; return value; };
    class Base { constructor(data) { mark("s"); this.data = data; } }
    class Child extends Base {
      field = mark("f", "field");
      constructor(prefix, { message: text = mark("d", prefix + fallback), cause: why, summary = mark("n", text) }, tail) {
        mark("p");
        super({ text, why, summary, tail });
      }
    }
    export const result = (() => {
      const first = new Child("start:", {
        get message() { return mark("a", undefined); },
        get cause() { return mark("b", 1); },
      }, "last");
      const second = new Child("skip:", { message: null, cause: 2, summary: false }, "done");
      const third = new Child("", { cause: 3, summary: 0 }, "x");
      if (Child.length !== 3) throw "wrong constructor length";
      return [first.data, second.data, third.data, trace.value];
    })();`,
    { "base.mjs": ["#0", "#1", "#2", "#3"], "child.mjs": ["#4"], "entry.mjs": ["#5"] },
    ["base.mjs", "child.mjs"]
  );
  assertCertifiedResult(test, [
    { text: "start:missing", why: 1, summary: "start:missing", tail: "last" },
    { text: null, why: 2, summary: false, tail: "done" },
    { text: "missing", why: 3, summary: 0, tail: "x" },
    "adbnpsfpsfdpsf",
  ]);
  assert.deepEqual(
    lower(test.files.sourceFile, test.files.specFile, test.out, ["--check-output"]).verdict,
    test.verdict
  );
  const childFile = path.join(test.out, "child.mjs");
  const code = readFileSync(childFile, "utf8");
  const changed = code.replace(/(["'])d\1/, "$1changed$1");
  assert.notEqual(code, changed);
  writeFileSync(childFile, changed);
  assert.equal(
    lower(test.files.sourceFile, test.files.specFile, test.out, ["--check-output"]).verdict.status,
    "rejected"
  );
}

if (includesCase("ordinary-parameter-scope")) {
  assertCertifiedResult(
    sequentialSplit(
      "ordinary-parameter-scope",
      `const label = "outer";
      function read({ message: value = label, capture = () => value }) {
        const label = "body";
        value += "!";
        return [value, capture(), label];
      }
      export const result = read({});`
    ),
    ["outer!", "outer!", "body"]
  );
}

if (includesCase("later-object-binding-tdz")) {
  const test = sequentialSplit(
    "later-object-binding-tdz",
    `const later = "outer";
    class Base {}
    class Child extends Base {
      constructor({ message: value = typeof later, later }) { super(); }
    }
    export const result = new Child({ later: 9 });`,
    { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3"] },
    ["base.mjs"]
  );
  assertRejected(test, /original threw during linked initialization/);
  assert.deepEqual(observe(test.files.sourceFile, "result"), { error: "ReferenceError" });
}

if (includesCase("derived-default-shadowed-by-prefix-var")) {
  const test = sequentialSplit(
    "derived-default-shadowed-by-prefix-var",
    `const label = "outer";
    class Base { constructor(value) { this.value = value; } }
    class Child extends Base {
      constructor({ message: value = label }) { var label = "body"; super(value); }
    }
    export const result = new Child({}).value;`,
    { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3"] },
    ["base.mjs"]
  );
  assertRejected(test, /var before super overlaps default parameters or their references/);
  assert.deepEqual(observe(test.files.sourceFile, "result"), { result: "outer" });
}
