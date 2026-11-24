import assert from "node:assert/strict";
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
} from "./linked_programs.mjs";

const records = Array.from({ length: 28 }, (_, index) => ({
  name: `item${index}`,
  fields: Array.from({ length: 10 }, (_, field) => [`part${field}`, `value-${index}-${field}`]),
}));
const member = (name, key) => ({
  type: "MemberExpression",
  object: id(name),
  property: id(key),
  computed: false,
  optional: false,
});
const declarations = records.map(({ name, fields }) =>
  variable("const", name, {
    type: "ObjectExpression",
    properties: fields.map(([key, value]) => property(key, literal(value))),
  })
);
declarations.push(
  variable("const", "selected", {
    type: "ArrayExpression",
    elements: [member("item0", "part0"), member("item27", "part9"), member("item1", "absent")],
  })
);
const names = [...records.map(({ name }) => name), "selected", "marker"];
const source = program([
  ...declarations,
  variable("const", "marker", literal("ready")),
  exportNamed(names.map((name) => [name, name])),
]);

const entry = "entry.mjs";
const order = ["marker.mjs", "records.mjs"];
const spec = {
  entry,
  modules: {
    "marker.mjs": [`#${declarations.length}`],
    "records.mjs": declarations.map((_, index) => `#${index}`),
    [entry]: [`#${declarations.length + 1}`],
  },
  order,
  sequential: true,
};
const dir = path.join(root, "small-records");
const { sourceFile, specFile } = prepare(dir, { source, provider: "", spec });
const out = path.join(dir, "out");
const { verdict, log } = lower(sourceFile, specFile, out);
assert.equal(verdict.status, "certified", log);
assert.equal(verdict.guarantee, "normal-completion", log);
assert.deepEqual(moduleEvaluationOrder(out, entry), [...order, entry]);
assert.deepEqual(lower(sourceFile, specFile, out, ["--check-output"]).verdict, verdict);

for (const file of [sourceFile, path.join(out, entry)]) {
  const namespace = await import(pathToFileURL(file).href);
  for (const { name, fields } of records) {
    const expected = Object.fromEntries(fields);
    assert.deepEqual(Object.keys(namespace[name]), Object.keys(expected));
    assert.deepEqual(Object.getOwnPropertyDescriptors(namespace[name]), Object.getOwnPropertyDescriptors(expected));
  }
  assert.deepEqual(namespace.selected, ["value-0-0", "value-27-9", undefined]);
  assert.equal(namespace.marker, "ready");
}
