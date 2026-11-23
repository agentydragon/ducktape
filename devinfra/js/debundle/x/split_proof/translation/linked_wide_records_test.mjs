import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  exportNamed,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  prepare,
  program,
  property,
  root,
  variable,
} from "./testing/linked_programs.mjs";

const fields = Array.from({ length: 705 }, (_, i) => property(`field${i}`, literal(`value${i}`)));
const record = { type: "ObjectExpression", properties: fields };
const member = (object, key, computed = false) => ({
  type: "MemberExpression",
  object: id(object),
  property: id(key),
  computed,
  optional: false,
});
const source = program([
  variable("const", "left", record),
  variable("const", "right", record),
  variable("const", "result", {
    type: "ArrayExpression",
    elements: [member("left", "field0"), member("right", "field704"), member("left", "missing")],
  }),
  exportNamed(["left", "right", "result"].map((name) => [name, name])),
]);
const entry = "entry.mjs";
const order = ["right.mjs", "left.mjs"];
const spec = {
  entry,
  modules: { "left.mjs": ["#0"], "right.mjs": ["#1"], [entry]: ["#2", "#3"] },
  order,
  sequential: true,
};
const dir = path.join(root, "wide-records");
const { sourceFile, specFile } = prepare(dir, { source, provider: "", spec });
const out = path.join(dir, "out");
const { verdict, log } = lower(sourceFile, specFile, out);
assert.equal(verdict.status, "certified", log);
assert.equal(verdict.guarantee, "normal-completion", log);
assert.deepEqual(moduleEvaluationOrder(out, entry), [...order, entry]);
assert.deepEqual(lower(sourceFile, specFile, out, ["--check-output"]).verdict, verdict);

for (const file of [sourceFile, path.join(out, entry)]) {
  const ns = await import(pathToFileURL(file).href);
  assert.deepEqual(Object.keys(ns), ["left", "result", "right"]);
  assert.deepEqual(ns.result, ["value0", "value704", undefined]);
  assert.deepEqual(
    Object.keys(ns.left),
    fields.map(({ key }) => key.name)
  );
  assert.deepEqual(ns.left, ns.right);
  assert.notEqual(ns.left, ns.right);
}

// A computed key does not get a per-key equation from the quoted syntax.
// The exact ordinary lookup handles a primitive key and still refuses object coercion.
for (const objectKey of [false, true]) {
  const key = objectKey ? { type: "ObjectExpression", properties: [] } : literal("field95");
  const source = program([
    variable("const", "table", { type: "ObjectExpression", properties: fields.slice(0, 96) }),
    variable("const", "key", key),
    variable("const", "result", member("table", "key", true)),
    exportNamed([["result", "result"]]),
  ]);
  const dir = path.join(root, `computed-record-key-${objectKey}`);
  const { sourceFile, specFile } = prepare(dir, {
    source,
    provider: "",
    spec: { entry, modules: { "table.mjs": ["#0"], [entry]: ["#1", "#2", "#3"] }, order: ["table.mjs"] },
  });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(sourceFile, specFile, out);
  if (objectKey) {
    assert.equal(verdict.status, "rejected", log);
    assert.match(verdict.reason, /object is converted to a primitive/);
    assert(!existsSync(out));
  } else {
    assert.equal(verdict.status, "certified", log);
    for (const file of [sourceFile, path.join(out, entry)]) {
      const { result } = await import(pathToFileURL(file).href);
      assert.equal(result, "value95");
    }
  }
}
