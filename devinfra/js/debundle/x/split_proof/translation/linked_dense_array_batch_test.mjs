import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  array,
  exportNamed,
  literal,
  lower,
  moduleEvaluationOrder,
  prepare,
  program,
  root,
  unary,
  variable,
} from "./testing/linked_programs.mjs";

const rowCount = 52;
const voidZero = unary("void", literal(0));
const rows = Array.from({ length: rowCount }, (_, index) =>
  array([
    literal(`row-${index}`),
    voidZero,
    array([array([literal(index)]), array([literal(index % 2 === 0)])]),
    array([]),
  ])
);
const source = program([
  variable("const", "rows", array(rows)),
  variable("const", "marker", literal("ready")),
  exportNamed([
    ["rows", "rows"],
    ["marker", "marker"],
  ]),
]);
const entry = "entry.mjs";
const order = ["marker.mjs", "rows.mjs"];
const spec = {
  entry,
  modules: { "marker.mjs": ["#1"], "rows.mjs": ["#0"], [entry]: ["#2"] },
  order,
  sequential: true,
};
const dir = path.join(root, "dense-array-batch");
const { sourceFile, specFile } = prepare(dir, { source, provider: "", spec });
const out = path.join(dir, "out");
const { verdict, log } = lower(sourceFile, specFile, out);
assert.equal(verdict.status, "certified", log);
assert.equal(verdict.guarantee, "normal-completion", log);
assert.deepEqual(moduleEvaluationOrder(out, entry), [...order, entry]);
assert.deepEqual(lower(sourceFile, specFile, out, ["--check-output"]).verdict, verdict);

const expectedRows = Array.from({ length: rowCount }, (_, index) => [
  `row-${index}`,
  undefined,
  [[index], [index % 2 === 0]],
  [],
]);
for (const file of [sourceFile, path.join(out, entry)]) {
  const namespace = await import(pathToFileURL(file).href);
  assert.deepEqual(namespace.rows, expectedRows);
  assert.equal(new Set(namespace.rows.map((row) => row[3])).size, rowCount);
  assert.equal(namespace.marker, "ready");
}
