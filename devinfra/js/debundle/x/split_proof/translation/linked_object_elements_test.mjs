import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  block,
  exportNamed,
  forcePreview,
  functionExpression,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  program,
  property,
  returns,
  sequentialSplit,
  variable,
} from "./testing/linked_programs.mjs";

const entry = "entry.mjs";
const inherited = sequentialSplit(
  "inherited-accessor",
  `
  const base = { get value() { return this.slot; }, set value(v) { this.slot = v; }, read() { return this.value; } };
  const run = () => {
    const obj = { __proto__: base, 1: "one" };
    obj.value = "written";
    const __proto__ = "own";
    const plain = { __proto__ };
    return [obj.read(), base.value === undefined, obj["1"], plain.__proto__];
  };
  export const result = run();`,
  { "base.mjs": ["#0"], [entry]: ["#1", "#2"] },
  ["base.mjs"]
);
assert.equal(inherited.verdict.status, "certified", inherited.log);
assert.equal(
  lower(inherited.files.sourceFile, inherited.files.specFile, inherited.out, ["--check-output"]).verdict.status,
  "certified"
);
for (const file of [inherited.files.sourceFile, path.join(inherited.out, entry)]) {
  assert.deepEqual(observe(file, "result"), { result: ["written", true, "one", "own"] });
}

// A getter captures its input without reading it at object creation. An eager
// data field with the same initializer reads the input as soon as its module runs.
for (const key of [1, 42]) {
  for (const lazy of [false, true]) {
    const field = {
      ...property("unused", lazy ? functionExpression([], block([returns(id("seed"))])) : id("seed")),
      key: literal(key),
      kind: lazy ? "get" : "init",
    };
    const source = program([
      variable("const", "seed", literal(key)),
      variable("const", "obj", { type: "ObjectExpression", properties: [field] }),
      variable("const", "result", {
        type: "MemberExpression",
        object: id("obj"),
        property: literal(String(key)),
        computed: true,
      }),
      exportNamed([["result", "result"]]),
    ]);
    const order = ["obj.mjs", "seed.mjs"];
    const test = sequentialSplit(
      `element-${key}-${lazy}`,
      source,
      { "seed.mjs": ["#0"], "obj.mjs": ["#1"], [entry]: ["#2", "#3"] },
      order
    );
    assert.deepEqual(observe(test.files.sourceFile, "result"), { result: key });
    let candidate = test.out;
    if (lazy) {
      assert.equal(test.verdict.status, "certified", test.log);
    } else {
      assert.equal(test.verdict.status, "rejected", test.log);
      assert(!existsSync(candidate));
      candidate = path.join(test.dir, "preview");
      forcePreview(test.files, candidate);
    }
    assert.deepEqual(moduleEvaluationOrder(candidate, entry), [...order, entry]);
    assert.deepEqual(
      observe(path.join(candidate, entry), "result"),
      lazy ? { result: key } : { error: "ReferenceError" }
    );
  }
}
