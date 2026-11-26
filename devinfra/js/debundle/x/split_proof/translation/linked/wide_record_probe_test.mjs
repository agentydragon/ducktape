import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";

const root = mkdtempSync(path.join(process.env.TEST_TMPDIR, "wide-record-probe-"));
const spec = path.join(root, "placement.json");
writeFileSync(
  spec,
  JSON.stringify({
    entry: "entry.mjs",
    modules: { "entry.mjs": ["#3"], "table.mjs": ["#0", "#2"], "rows.mjs": ["#1"] },
    order: ["table.mjs", "rows.mjs"],
    sequential: true,
  })
);
const table = Array.from({ length: 705 }, (_, i) => `field${i}: "value${i}"`).join(", ");

for (const changed of [false, true]) {
  const source = path.join(root, changed ? "changed.mjs" : "source.mjs");
  const marker = changed ? '(table.field0 = "changed", "ready")' : '"ready"';
  writeFileSync(
    source,
    `const table = { ${table} };
const rows = [table.field0, [table.field0]];
const marker = ${marker};
export { table, rows, marker };\n`
  );
  // The diagnostic order check refuses both placements. The probe distinguishes them.
  await withSplitPreview({ source, spec, expectedExit: 1 }, async ({ directory, entryFile }) => {
    const original = await import(pathToFileURL(source).href);
    const candidate = await import(pathToFileURL(entryFile).href);
    const candidateValue = changed ? "changed" : "value0";
    assert.deepEqual(original.rows, ["value0", ["value0"]]);
    assert.deepEqual(candidate.rows, [candidateValue, [candidateValue]]);
    assert.equal(candidate.marker, original.marker);
    const report = probeLinkedModel({
      source,
      spec,
      directory,
      mismatchFuel: changed ? 64 + 705 : undefined,
      observe: [
        ["table", "field704"],
        ["rows", "1", "0"],
      ],
    });
    if (changed) {
      assert.equal(report.reason_code, "probe_mismatch");
      assert.deepEqual(report.observation, {
        kind: "value",
        path: ["rows", "0"],
        original: '"value0"',
        split: '"changed"',
      });
      assert.equal(report.status, "refused");
      assert.equal(report.fuel, 64 + 705);
      assert.deepEqual(report.observed, [
        { path: ["table", "field704"], original: '"value704"', split: '"value704"' },
        { path: ["rows", "1", "0"], original: '"value0"', split: '"changed"' },
      ]);
    } else {
      assert.equal(report.status, "matched");
      assert.equal(report.fuel, 64 + 705);
      assert.deepEqual(report.assumptions, []);
      assert.deepEqual(report.observed, [
        { path: ["table", "field704"], original: '"value704"', split: '"value704"' },
        { path: ["rows", "1", "0"], original: '"value0"', split: '"value0"' },
      ]);
    }
  });
}
