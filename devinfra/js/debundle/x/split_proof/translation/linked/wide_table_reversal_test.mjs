import assert from "node:assert/strict";
import path from "node:path";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { pathToFileURL } from "node:url";
import {
  array,
  arrow,
  caseShard,
  exportNamed,
  id,
  literal,
  member,
  moduleEvaluationOrder,
  prepare,
  program,
  property,
  root,
  unary,
  variable,
} from "./harness.mjs";

const fieldCount = Number(process.env.LINKED_TABLE_FIELDS ?? 705);
const rowCount = Number(process.env.LINKED_TABLE_ROWS ?? 44);
assert(Number.isSafeInteger(fieldCount) && fieldCount >= 17);
assert(Number.isSafeInteger(rowCount) && rowCount >= 1);
const fields = Array.from({ length: fieldCount }, (_, index) => [`field${index}`, `value${index}`]);
const indexOf = (index) => (fieldCount - 17 + index) % fieldCount;
const readField = (name, key) => member(id(name), id(key));
const read = (index) => readField("table", fields[indexOf(index)][0]);
const value = (index) => fields[indexOf(index)][1];
const offsets = [1, 2, 3, 4];
const voidZero = unary("void", literal(0));
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
  variable("const", "left", array([readField("option0", "part0")])),
  variable("const", "right", array([readField("option1", "part0"), readField("option1", "part1")]))
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
const files = prepare(dir, { source: tableProgram(literal("ready")), provider: "", spec });
const { sourceFile, specFile } = files;
const out = path.join(dir, "out");
const runCase = caseShard();
if (runCase("public refusal and preview runtime")) {
  const manifest = writeDeployment(sourceFile, specFile, out);
  assertPublicEmissionRefused(manifest, out);
  await withSplitPreview({ source: sourceFile, spec: specFile, expectedExit: 1 }, async ({ directory, entryFile }) => {
    assert.deepEqual(moduleEvaluationOrder(directory, entry), [...order, entry]);
    const model = probeLinkedModel({
      source: sourceFile,
      spec: specFile,
      directory,
      observe: [["table", fields[fieldCount - 1][0]], ["rows", "0", "2"], ["marker"]],
    });
    assert.equal(model.status, "matched");
    assert.equal(model.fuel, 64 + fieldCount);
    assert.deepEqual(model.assumptions, []);
    assert.deepEqual(model.host_refs, []);
    assert.deepEqual(model.observed, [
      {
        path: ["table", fields[fieldCount - 1][0]],
        original: JSON.stringify(fields[fieldCount - 1][1]),
        split: JSON.stringify(fields[fieldCount - 1][1]),
      },
      { path: ["rows", "0", "2"], original: JSON.stringify(value(0)), split: JSON.stringify(value(0)) },
      { path: ["marker"], original: '"ready"', split: '"ready"' },
    ]);

    const expectedRows = Array.from({ length: rowCount }, (_, index) => [
      `item-${index}`,
      undefined,
      value(index),
      [],
      offsets.map((offset) => value(index + offset)),
    ]);
    for (const file of [sourceFile, entryFile]) {
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

    assertPublicEmissionRefused(manifest, directory);
    assertPublicRecheckRefused(manifest, directory);
  });
}

if (runCase("reject changed schedule")) {
  // Keep the placement and marker value while writing the field captured in the first row.
  const changedMarker = {
    type: "SequenceExpression",
    expressions: [
      { type: "AssignmentExpression", operator: "=", left: read(0), right: literal("changed") },
      literal("ready"),
    ],
  };
  const control = prepare(path.join(dir, "control"), { source: tableProgram(changedMarker), provider: "", spec });
  const refusedOut = path.join(dir, "refused");
  const manifest = writeDeployment(control.sourceFile, control.specFile, refusedOut);
  assertPublicEmissionRefused(manifest, refusedOut);
  await withSplitPreview(
    { source: control.sourceFile, spec: control.specFile, expectedExit: 1 },
    async ({ directory, entryFile }) => {
      const model = probeLinkedModel({
        source: control.sourceFile,
        spec: control.specFile,
        directory,
        observe: [["rows", "0", "2"]],
        mismatchFuel: 64 + fieldCount,
      });
      assert.equal(model.status, "refused");
      assert.equal(model.reason_code, "probe_mismatch");
      assert.equal(model.fuel, 64 + fieldCount);
      assert.deepEqual(model.observed, [
        {
          path: ["rows", "0", "2"],
          original: JSON.stringify(value(0)),
          split: '"changed"',
        },
      ]);
      const before = await import(pathToFileURL(control.sourceFile).href);
      const after = await import(pathToFileURL(entryFile).href);
      assert.deepEqual(moduleEvaluationOrder(directory, entry), [...order, entry]);
      assert.equal(before.rows[0][2], value(0));
      assert.equal(after.rows[0][2], "changed");
      assertPublicEmissionRefused(manifest, directory);
      assertPublicRecheckRefused(manifest, directory);
    }
  );
}
