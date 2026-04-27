import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { FIXTURE_UI_VERSION, createWebFixtureRoots, readUtf8, writeSnapshotFixture } from "../test_support/fixtures.mjs";
import { renameBindingsInArtifact } from "../rename/bindings.mjs";
import { buildNormalizedPipelineArtifactFromSnapshot } from "../test_support/pipeline_fixtures.mjs";
import { extractOwnerClosurePlanReport } from "./closure_report.mjs";

const SPLIT_ENTRY_FILE = "entry.js";

test("extractOwnerClosurePlanReport emits per-chunk reports and summary with byte estimates", async () => {
  const { extractedRoot, root, snapshotRoot } = createWebFixtureRoots("debundle-owner-closure-report-test-");
  const source = `const helperSeed = 1;
function readHelperSeed() {
  return helperSeed;
}
const composed = readHelperSeed() + 2;
function render() {
  return composed;
}
console.log(JSON.stringify({ value: render() }));
export { render as b };
`;

  writeSnapshotFixture({
    assetSummary: {
      entryPoints: { html: "index.html", js: ["static/a.js"] },
      uiVersion: FIXTURE_UI_VERSION,
    },
    extractedRoot,
    files: {
      "static/a.js": source,
    },
    html: `<!doctype html><html><head><script type="module" src="/static/a.js"></script></head><body></body></html>\n`,
    jsFiles: ["static/a.js"],
    snapshotRoot,
  });

  const splitResult = await buildNormalizedPipelineArtifactFromSnapshot({
    jsListPath: join(extractedRoot, "js-files.txt"),
    snapshotRoot,
  });
  const renamed = renameBindingsInArtifact({
    artifact: splitResult.artifact,
    operations: [
      {
        id: "rename_fixture_read_helper_seed",
        operation: "rename_binding",
        selector: {
          binding: {
            kind: "FunctionDeclaration",
            name: "readHelperSeed",
          },
          chunkId: "static/a",
          file: SPLIT_ENTRY_FILE,
          owner: {
            id: "owner_00001",
            line: 2,
          },
        },
        target: {
          name: "readSeed",
        },
        fingerprint: {
          bodyContains: "return helperSeed;",
          paramsCount: 0,
        },
      },
    ],
  });

  const reportRoot = join(root, "report");
  const summary = extractOwnerClosurePlanReport({
    artifact: renamed.artifact,
    force: true,
    inputRoot: join(root, "renamed"),
    outDir: reportRoot,
    uiVersion: FIXTURE_UI_VERSION,
  });

  assert.equal(summary.kind, "js.ordered_init_owner_closure_report_summary");
  assert.equal(summary.lowering, "staged_shell");
  assert.equal(summary.counts.chunks, 1);
  assert.equal(summary.counts.selectedBatchPlans, 1);
  assert.ok(summary.totals.runtimeBytes > 0);
  assert.ok(summary.totals.selectedLoweredRuntimeBytes >= 0);
  assert.equal(
    summary.totals.approxRemainingRuntimeBytes,
    summary.totals.runtimeBytes - summary.totals.selectedLoweredRuntimeBytes
  );

  const chunkReportPath = join(reportRoot, "static", "a.json");
  assert.equal(existsSync(chunkReportPath), true);
  const chunkReport = JSON.parse(readUtf8(chunkReportPath));
  assert.equal(chunkReport.kind, "js.ordered_init_owner_closure_report");
  assert.equal(chunkReport.chunkId, "static/a");
  assert.equal(chunkReport.counts.selectedBatchPlans, 1);
  assert.ok(chunkReport.counts.candidateBatchPlans >= chunkReport.counts.selectedBatchPlans);
  assert.ok(chunkReport.closurePlans.some((plan) => plan.seedMemberNames.includes("render")));
  assert.ok(chunkReport.closurePlans.every((plan) => typeof plan.contiguousEnvelopeOwnerCount === "number"));
  assert.ok(chunkReport.selectedBatchPlans[0].semanticMemberNamePreview.includes("readSeed"));
  assert.ok(chunkReport.selectedBatchPlans[0].loweredRuntimeBytes >= 0);
  assert.equal(
    chunkReport.totals.approxRemainingRuntimeBytes,
    chunkReport.runtimeBytes - chunkReport.totals.selectedLoweredRuntimeBytes
  );
});

test("extractOwnerClosurePlanReport captures staged-shell lowering details", async () => {
  const { extractedRoot, root, snapshotRoot } = createWebFixtureRoots("debundle-owner-closure-staged-report-test-");
  const source = `const helperSeed = 1;
function readHelperSeed() {
  return helperSeed;
}
console.log("graph-barrier");
const composed = readHelperSeed() + 2;
function render() {
  return composed;
}
console.log(JSON.stringify({ value: render() }));
export { render as b };
`;

  writeSnapshotFixture({
    assetSummary: {
      entryPoints: { html: "index.html", js: ["static/a.js"] },
      uiVersion: FIXTURE_UI_VERSION,
    },
    extractedRoot,
    files: {
      "static/a.js": source,
    },
    html: `<!doctype html><html><head><script type="module" src="/static/a.js"></script></head><body></body></html>\n`,
    jsFiles: ["static/a.js"],
    snapshotRoot,
  });

  const splitResult = await buildNormalizedPipelineArtifactFromSnapshot({
    jsListPath: join(extractedRoot, "js-files.txt"),
    snapshotRoot,
  });
  const renamed = renameBindingsInArtifact({
    artifact: splitResult.artifact,
    operations: [],
  });

  const reportRoot = join(root, "report");
  const summary = extractOwnerClosurePlanReport({
    artifact: renamed.artifact,
    force: true,
    inputRoot: join(root, "renamed"),
    outDir: reportRoot,
    uiVersion: FIXTURE_UI_VERSION,
  });

  assert.equal(summary.lowering, "staged_shell");
  assert.equal(summary.counts.selectedBatchPlans, 1);
  assert.ok(summary.totals.selectedLoweredRuntimeBytes >= 0);

  const chunkReport = JSON.parse(readUtf8(join(reportRoot, "static", "a.json")));
  assert.equal(chunkReport.lowering, "staged_shell");
  assert.equal(chunkReport.selectedBatchPlans[0].stageCount, 2);
  assert.deepEqual(chunkReport.selectedBatchPlans[0].shellItemPreview, ["side_effect_00000"]);
  assert.equal(chunkReport.selectedBatchPlans[0].attachedItemCount, 1);
  assert.ok(chunkReport.selectedBatchPlans[0].loweredRuntimeBytes >= 0);
});
