import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  assertCertified,
  assertRejectedOrOrderKeptSlice,
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
} from "./harness.mjs";

const fields = Array.from({ length: 705 }, (_, i) => property(`field${i}`, literal(`value${i}`)));
const record = { type: "ObjectExpression", properties: fields };
const member = (object, key, computed = false) => ({
  type: "MemberExpression",
  object: id(object),
  property: id(key),
  computed,
  optional: false,
});
const sampledFields = [0, 1, 117, 235, 352, 469, 587, 704];
const source = program([
  variable("const", "left", record),
  variable("const", "right", record),
  variable("const", "computedKey", literal("field263")),
  variable("const", "result", {
    type: "ArrayExpression",
    elements: [
      ...sampledFields.map((i) => member(i % 2 ? "right" : "left", `field${i}`)),
      member("left", "missing"),
      member("right", "computedKey", true),
    ],
  }),
  exportNamed(["left", "right", "result"].map((name) => [name, name])),
]);
const entry = "entry.mjs";
const order = ["right.mjs", "left.mjs"];
const spec = {
  entry,
  modules: { "left.mjs": ["#0"], "right.mjs": ["#1"], [entry]: ["#2", "#3", "#4"] },
  order,
  sequential: true,
};
const dir = path.join(root, "wide-records");
const { sourceFile, specFile } = prepare(dir, { source, provider: "", spec });
const out = path.join(dir, "out");
const { verdict, log } = lower(sourceFile, specFile, out);
assertCertified(verdict, log);
assert.deepEqual(moduleEvaluationOrder(out, entry), [...order, entry]);
assert.deepEqual(lower(sourceFile, specFile, out, ["--check-output"]).verdict, verdict);

for (const file of [sourceFile, path.join(out, entry)]) {
  const ns = await import(pathToFileURL(file).href);
  assert.deepEqual(Object.keys(ns), ["left", "result", "right"]);
  assert.deepEqual(ns.result, [...sampledFields.map((i) => `value${i}`), undefined, "value263"]);
  assert.deepEqual(
    Object.keys(ns.left),
    fields.map(({ key }) => key.name)
  );
  assert.deepEqual(ns.left, ns.right);
  assert.notEqual(ns.left, ns.right);
}

// A computed key does not get a per-key equation from the quoted syntax.
// Duplicate static keys keep ordinary object-literal replacement semantics.
for (const kind of ["string", "object", "duplicate"]) {
  const objectKey = kind === "object";
  const key = objectKey ? { type: "ObjectExpression", properties: [] } : literal("field95");
  const source = program([
    variable("const", "table", {
      type: "ObjectExpression",
      properties:
        kind === "duplicate" ? [...fields.slice(0, 96), property("field2", literal("last"))] : fields.slice(0, 96),
    }),
    variable("const", "key", key),
    variable("const", "result", {
      type: "ArrayExpression",
      elements: [member("table", "key", true), ...Array.from({ length: 8 }, (_, i) => member("table", `field${i}`))],
    }),
    exportNamed([["result", "result"]]),
  ]);
  const dir = path.join(root, `computed-record-key-${kind}`);
  const { sourceFile, specFile } = prepare(dir, {
    source,
    provider: "",
    spec: { entry, modules: { "table.mjs": ["#0"], [entry]: ["#1", "#2", "#3"] }, order: ["table.mjs"] },
  });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(sourceFile, specFile, out);
  if (kind === "duplicate") {
    assertCertified(verdict, log);
    for (const file of [sourceFile, path.join(out, entry)]) {
      const { result } = await import(pathToFileURL(file).href);
      assert.deepEqual(result, [
        "value95",
        "value0",
        "value1",
        "last",
        "value3",
        "value4",
        "value5",
        "value6",
        "value7",
      ]);
    }
  } else if (objectKey) {
    const orderKept = assertRejectedOrOrderKeptSlice({ verdict, log, out }, /object is converted to a primitive/);
    if (orderKept) {
      for (const file of [sourceFile, path.join(out, entry)]) {
        const { result } = await import(pathToFileURL(file).href);
        assert.deepEqual(result, [undefined, ...Array.from({ length: 8 }, (_, i) => `value${i}`)]);
      }
    }
  } else {
    assert.equal(verdict.status, "certified", log);
    for (const file of [sourceFile, path.join(out, entry)]) {
      const { result } = await import(pathToFileURL(file).href);
      assert.deepEqual(result, [
        "value95",
        "value0",
        "value1",
        "value2",
        "value3",
        "value4",
        "value5",
        "value6",
        "value7",
      ]);
    }
  }
}
