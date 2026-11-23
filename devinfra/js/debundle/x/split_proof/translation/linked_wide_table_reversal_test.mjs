import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  arrow,
  exportNamed,
  forcePreview,
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

const fieldCount = Number(process.env.LINKED_TABLE_FIELDS ?? 705);
const rowCount = Number(process.env.LINKED_TABLE_ROWS ?? 44);
assert(Number.isSafeInteger(fieldCount) && fieldCount >= 17);
assert(Number.isSafeInteger(rowCount) && rowCount >= 1);
const fields = Array.from({ length: fieldCount }, (_, index) => [`field${index}`, `value${index}`]);
const indexOf = (index) => (fieldCount - 17 + index) % fieldCount;
const member = (name, key) => ({
  type: "MemberExpression",
  object: id(name),
  property: id(key),
  computed: false,
  optional: false,
});
const read = (index) => member("table", fields[indexOf(index)][0]);
const value = (index) => fields[indexOf(index)][1];
const offsets = [1, 2, 3, 4];
const array = (elements) => ({ type: "ArrayExpression", elements });
const voidZero = { type: "UnaryExpression", operator: "void", prefix: true, argument: literal(0) };
const rows = Array.from({ length: rowCount }, (_, index) =>
  array([
    literal(`item-${index}`),
    voidZero,
    read(index),
    array([]),
    array(offsets.map((offset) => read(index + offset))),
  ])
);
const optionWidths = [1, 2, 3, 4, 10, 2, 3, 5, 3, 4, 2, 4, 1, 3, 3, 5, 2, 5, 3, 4];
const options = optionWidths.map((width, index) => ({
  name: `option${index}`,
  fields: Array.from({ length: width }, (_, field) => [`part${field}`, `option-${index}-${field}`]),
}));
const optionUnits = options.map(({ name, fields }) =>
  variable("const", name, {
    type: "ObjectExpression",
    properties: fields.map(([key, value]) => property(key, literal(value))),
  })
);
optionUnits.push(
  variable("const", "left", array([member("option0", "part0")])),
  variable("const", "right", array([member("option1", "part0"), member("option1", "part1")]))
);
const exportNames = [
  "table",
  "quick",
  "rows",
  ...options.map(({ name }) => name),
  "left",
  "right",
  "marker",
  "getMarker",
];
function tableProgram(marker) {
  return program([
    variable("const", "table", {
      type: "ObjectExpression",
      properties: fields.map(([name, val]) => property(name, literal(val))),
    }),
    variable("const", "quick", array([0, 1, 2].map(read))),
    variable("const", "rows", array(rows)),
    ...optionUnits,
    variable("const", "marker", marker),
    variable("const", "getMarker", arrow([], id("marker"))),
    exportNamed(exportNames.map((name) => [name, name])),
  ]);
}

const entry = "entry.mjs";
const order = ["lookup.mjs", "options.mjs", "rows.mjs"];
// Both trailing initializers cross the option declarations and the rows initializer.
// The options also cross the rows initializer: 2 × 23 + 22 eager reversals.
const markerIndex = 3 + optionUnits.length;
const spec = {
  entry,
  modules: {
    "lookup.mjs": ["#0", "#1", `#${markerIndex}`, `#${markerIndex + 1}`],
    "options.mjs": optionUnits.map((_, index) => `#${index + 3}`),
    "rows.mjs": ["#2"],
    [entry]: [`#${markerIndex + 2}`],
  },
  order,
  sequential: true,
};
const dir = path.join(root, "dense-lookup-arrays");
const { sourceFile, specFile } = prepare(dir, { source: tableProgram(literal("ready")), provider: "", spec });
const out = path.join(dir, "out");
const { verdict, log } = lower(sourceFile, specFile, out);
assert.equal(verdict.status, "certified", log);
assert.equal(verdict.guarantee, "normal-completion", log);
assert.deepEqual(moduleEvaluationOrder(out, entry), [...order, entry]);
assert.deepEqual(lower(sourceFile, specFile, out, ["--check-output"]).verdict, verdict);

const expectedRows = Array.from({ length: rowCount }, (_, index) => [
  `item-${index}`,
  undefined,
  value(index),
  [],
  offsets.map((offset) => value(index + offset)),
]);
for (const file of [sourceFile, path.join(out, entry)]) {
  const namespace = await import(pathToFileURL(file).href);
  assert.deepEqual(Object.keys(namespace), [...exportNames].sort());
  assert.deepEqual(Object.entries(namespace.table), fields);
  assert.deepEqual(namespace.quick, [0, 1, 2].map(value));
  assert.deepEqual(namespace.rows, expectedRows);
  assert.equal(new Set(namespace.rows.map((row) => row[3])).size, rowCount);
  for (const { name, fields } of options) assert.deepEqual(Object.entries(namespace[name]), fields);
  assert.deepEqual(namespace.left, ["option-0-0"]);
  assert.deepEqual(namespace.right, ["option-1-0", "option-1-1"]);
  assert.equal(namespace.marker, "ready");
  assert.equal(namespace.getMarker(), "ready");
}

// Keep the placement and the marker value, but write the field captured in the first row.
const changedMarker = {
  type: "SequenceExpression",
  expressions: [
    { type: "AssignmentExpression", operator: "=", left: read(0), right: literal("changed") },
    literal("ready"),
  ],
};
const control = prepare(path.join(dir, "control"), { source: tableProgram(changedMarker), provider: "", spec });
const preview = path.join(dir, "preview");
forcePreview(control, preview);
const before = await import(pathToFileURL(control.sourceFile).href);
const after = await import(pathToFileURL(path.join(preview, entry)).href);
assert.deepEqual(moduleEvaluationOrder(preview, entry), [...order, entry]);
assert.equal(before.rows[0][2], value(0));
assert.equal(after.rows[0][2], "changed");
for (const result of [
  lower(control.sourceFile, control.specFile, path.join(dir, "refused")),
  lower(control.sourceFile, control.specFile, preview, ["--check-output"]),
]) {
  assert.equal(result.verdict.status, "rejected", result.log);
  assert.match(result.verdict.reason, /different final states/);
}
