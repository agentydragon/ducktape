import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  assertCertified,
  array,
  caseShard,
  exportNamed,
  forcePreview,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  prepare,
  program,
  root,
  unary,
  variable,
} from "./harness.mjs";

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
const files = prepare(dir, { source, provider: "", spec });
const { sourceFile, specFile } = files;
const out = path.join(dir, "out");
const runCase = caseShard();
const emit = runCase("certified emission and runtime");
if (emit) {
  const { verdict, log } = lower(sourceFile, specFile, out);
  assertCertified(verdict, log);
  assert.equal(verdict.strategy, "compiled-replay-premised", log);
  assert.deepEqual(moduleEvaluationOrder(out, entry), [...order, entry]);

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
}

if (runCase("exact disk verification")) {
  if (!emit) forcePreview(files, out);
  const { verdict, log } = lower(sourceFile, specFile, out, ["--check-output"]);
  assertCertified(verdict, log);
  assert.equal(verdict.strategy, "compiled-replay-premised", log);
}

// The operand of void must still be evaluated. In source order this read hits
// the TDZ; moving marker first would make the same array initialize normally.
if (runCase("void-operand-tdz")) {
  const firstRow = array([literal("row-0"), unary("void", id("marker"))]);
  const badSource = program([
    variable("const", "rows", array([firstRow, ...rows.slice(1)])),
    variable("const", "marker", literal("ready")),
    exportNamed([
      ["rows", "rows"],
      ["marker", "marker"],
    ]),
  ]);
  const badFiles = prepare(path.join(root, "void-operand-tdz"), { source: badSource, provider: "", spec });
  const badOut = path.join(root, "void-operand-tdz", "out");
  const { verdict, log } = lower(badFiles.sourceFile, badFiles.specFile, badOut);
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, /original threw during linked initialization/, log);
  assert(!existsSync(badOut));
  assert.deepEqual(observe(badFiles.sourceFile, "rows"), { error: "ReferenceError" });
}
