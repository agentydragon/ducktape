import assert from "node:assert/strict";
import { join } from "node:path";
import test from "node:test";
import { analyzeRuntimeBoundaryCode } from "../analysis/boundary.mjs";
import { createTempFixtureRoot, runNodeScript, writeRunnableFixture } from "../test_support/fixtures.mjs";
import {
  buildOrderedInitOwnerClosureOperations,
  packOrderedInitOwnerClosures,
  planOrderedInitOwnerClosureExtractions,
} from "./decl_graph.mjs";
import { extractOrderedInitRegionsInCode } from "./init_region.mjs";

test("owner closure operations can omit selector.file and run against a non-runtime entry file", () => {
  const source = `const helperSeed = 1;
function readHelperSeed() { return helperSeed; }
const composed = readHelperSeed() + 2;
function render() { return composed; }
console.log(render());
export { render as publicRender };
`;

  const plan = ownerClosurePlanForCode(source);
  const batchPlan = packOrderedInitOwnerClosures(plan).batchPlans.find((candidate) =>
    candidate.memberNames.includes("render")
  );
  const operations = buildOrderedInitOwnerClosureOperations({ batchPlans: [batchPlan] }, {
    chunkId: "static/app",
    idPrefix: "entry_owner",
    initPrefix: "init_entry_owner_",
    targetDir: "regions",
  });

  assert.equal("file" in operations[0].selector, false);

  const result = extractOrderedInitRegionsInCode(source, operations, { file: "entry.js" });
  assert.match(result.files.get("entry.js"), /from "\.\/regions\/owner_closure_/);
  assertRunnableEquivalent({
    entryFile: "entry.js",
    prefix: "debundle-owner-closure-entry-file-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("owner closure planner keeps semantic closures compact before staged-shell packing", () => {
  const plan = ownerClosurePlanForCode(`const helperSeed = 1;
function readHelperSeed() { return helperSeed; }

console.log("barrier");

const composed = readHelperSeed() + 1;
function render() { return composed; }
`);

  const renderPlan = findOwnerClosurePlanByName(plan, "render");
  assert.deepEqual(renderPlan.blockingReasons, []);
  assert.deepEqual(renderPlan.memberNames, ["composed", "helperSeed", "readHelperSeed", "render"]);
  assert.deepEqual(renderPlan.ownerIds, renderPlan.requiredClosureOwnerIds);
});

test("owner closure planner keeps report-only envelope data off the default runtime plan", () => {
  const source = `const helperSeed = 1;
function readHelperSeed() { return helperSeed; }

console.log("barrier");

const composed = readHelperSeed() + 1;
function render() { return composed; }
`;

  const runtimePlan = ownerClosurePlanForCode(source);
  const reportPlan = ownerClosurePlanForCode(source, {
    includeReportDetails: true,
  });

  const runtimeRenderPlan = findOwnerClosurePlanByName(runtimePlan, "render");
  const reportRenderPlan = findOwnerClosurePlanByName(reportPlan, "render");

  assert.equal("contiguousEnvelopeOwnerIds" in runtimeRenderPlan, false);
  assert.equal("envelopeAddedOwnerIds" in runtimeRenderPlan, false);
  assert.deepEqual(reportRenderPlan.contiguousEnvelopeOwnerIds, runtimeRenderPlan.ownerIds);
  assert.deepEqual(reportRenderPlan.envelopeAddedOwnerIds, []);
});

test("owner closure packer prefers the larger non-overlapping lowered closures", () => {
  const plan = ownerClosurePlanForCode(`const helperSeed = 1;
function readHelperSeed() { return helperSeed; }
const composed = readHelperSeed() + 1;
function render() { return composed; }

console.log("barrier");

const localOnly = 2;
function readLocalOnly() { return localOnly; }
`);

  const batchPlan = packOrderedInitOwnerClosures(plan);
  assert.equal(batchPlan.batchPlans.length, 2);
  const renderBatch = findOwnerClosureBatchPlanByName(batchPlan, "render");
  const localBatch = findOwnerClosureBatchPlanByName(batchPlan, "readLocalOnly");
  assert.deepEqual(renderBatch.semanticMemberNames, ["composed", "helperSeed", "readHelperSeed", "render"]);
  assert.deepEqual(localBatch.semanticMemberNames, ["localOnly", "readLocalOnly"]);
  assert.equal(batchPlan.batchPlans.some((candidate) => candidate.semanticMemberNames.includes("readHelperSeed")), true);
  assert.equal(
    batchPlan.batchPlans.some(
      (candidate) =>
        candidate.semanticMemberNames.includes("readHelperSeed") &&
        !candidate.semanticMemberNames.includes("render")
    ),
    false
  );
});

test("owner closure planner emits runnable extraction operations for the current lowering", () => {
  const source = `const helperSeed = 1;
function readHelperSeed() { return helperSeed; }
const composed = readHelperSeed() + 2;
function render() { return composed; }
console.log(JSON.stringify({ value: render() }));
export { render as publicRender };
`;

  const plan = ownerClosurePlanForCode(source);
  const batchPlan = packOrderedInitOwnerClosures(plan);
  const operations = buildOrderedInitOwnerClosureOperations(batchPlan, {
    chunkId: "static/app",
    file: "runtime.js",
    filePrefix: "owner_",
    idPrefix: "fixture_owner",
    initPrefix: "init_fixture_owner_",
    targetDir: "regions",
  });
  const result = extractOrderedInitRegionsInCode(source, operations);

  const extractedFiles = [...result.files.keys()].filter((file) => file.startsWith("regions/owner_"));
  assert.equal(extractedFiles.length, 1);
  assert.match(result.code, /from "\.\/regions\/owner_owner_closure_/);
  assertRunnableEquivalent({
    prefix: "debundle-owner-closure-pass-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("owner closure planner supports staged-shell lowering across retained top-level barriers", () => {
  const source = `const helperSeed = 1;
function readHelperSeed() { return helperSeed; }
console.log("graph-barrier");
const composed = readHelperSeed() + 2;
function render() { return composed; }
console.log(JSON.stringify({ helper: readHelperSeed(), value: render() }));
export { render as publicRender };
`;

  const plan = ownerClosurePlanForCode(source);
  const batchPlan = packOrderedInitOwnerClosures(plan, {
    lowering: "staged_shell",
  });
  const renderBatch = findOwnerClosureBatchPlanByName(batchPlan, "render");
  assert.equal(renderBatch.lowering, "staged_shell");
  assert.equal(renderBatch.blockingReasons.length, 0);
  assert.equal(renderBatch.stageRuns.length, 2);
  assert.deepEqual(renderBatch.shellItemIds, ["side_effect_00000"]);

  const operations = buildOrderedInitOwnerClosureOperations(batchPlan, {
    chunkId: "static/app",
    file: "runtime.js",
    filePrefix: "owner_",
    idPrefix: "fixture_staged_owner",
    initPrefix: "init_fixture_staged_owner_",
    lowering: "staged_shell",
    targetDir: "regions",
  });
  const result = extractOrderedInitRegionsInCode(source, operations);

  const extractedFiles = [...result.files.keys()].filter((file) => file.startsWith("regions/owner_"));
  assert.equal(extractedFiles.length, 1);
  assert.match(result.code, /init_fixture_staged_owner_owner_closure_.*_stage_0/);
  assert.match(result.code, /init_fixture_staged_owner_owner_closure_.*_stage_1/);
  assert.match(result.files.get(extractedFiles[0]), /export function init_fixture_staged_owner_owner_closure_.*_stage_0/s);
  assert.match(result.files.get(extractedFiles[0]), /export function init_fixture_staged_owner_owner_closure_.*_stage_1/s);
  assertRunnableEquivalent({
    prefix: "debundle-owner-closure-staged-pass-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("owner closure pass expands staged-shell extractions inside the extractor", () => {
  const source = `const helperSeed = 1;
function readHelperSeed() { return helperSeed; }
console.log("graph-barrier");
const composed = readHelperSeed() + 2;
function render() { return composed; }
console.log(JSON.stringify({ helper: readHelperSeed(), value: render() }));
export { render as publicRender };
`;

  const result = extractOrderedInitRegionsInCode(source, [
    {
      id: "fixture_owner_closure_pass",
      operation: "extract_ordered_init_owner_closure_pass",
      selector: {
        chunkId: "static/app",
        file: "runtime.js",
      },
      target: {
        dir: "regions",
        filePrefix: "owner_",
        initPrefix: "init_fixture_staged_owner_",
      },
      options: {
        lowering: "staged_shell",
      },
    },
  ], {
    chunkId: "static/app",
    file: "runtime.js",
  });

  const extractedFiles = [...result.files.keys()].filter((file) => file.startsWith("regions/owner_"));
  assert.equal(extractedFiles.length, 1);
  assert.match(result.code, /init_fixture_staged_owner_owner_closure_.*_stage_0/);
  assert.match(result.code, /init_fixture_staged_owner_owner_closure_.*_stage_1/);
  assertRunnableEquivalent({
    prefix: "debundle-owner-closure-pass-expansion-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("staged-shell lowering can attach replayable writer side effects to the extracted closure", () => {
  const source = `let enabled = false;
const state = { value: 1 };
console.log("writer-barrier");
globalThis.turnOn = () => {
  enabled = true;
  state.value += 2;
};
function render() {
  return enabled ? state.value : -1;
}
globalThis.turnOn();
console.log(JSON.stringify({ enabled, value: render() }));
export { render as publicRender };
`;

  const plan = ownerClosurePlanForCode(source);
  const batchPlan = packOrderedInitOwnerClosures(plan, {
    lowering: "staged_shell",
  });
  const renderBatch = findOwnerClosureBatchPlanByName(batchPlan, "render");
  assert.equal(renderBatch.lowering, "staged_shell");
  assert.equal(renderBatch.blockingReasons.length, 0);
  assert.deepEqual(renderBatch.shellItemIds, ["side_effect_00000", "side_effect_00002"]);
  assert.deepEqual(renderBatch.attachedItemIds, ["side_effect_00001", "side_effect_00003"]);
  assert.equal(renderBatch.stageRuns.length, 3);

  const operations = buildOrderedInitOwnerClosureOperations(batchPlan, {
    chunkId: "static/app",
    file: "runtime.js",
    filePrefix: "owner_",
    idPrefix: "fixture_attached_writer_owner",
    initPrefix: "init_fixture_attached_writer_owner_",
    lowering: "staged_shell",
    targetDir: "regions",
  });
  const result = extractOrderedInitRegionsInCode(source, operations);

  const extractedFiles = [...result.files.keys()].filter((file) => file.startsWith("regions/owner_"));
  assert.equal(extractedFiles.length, 1);
  assert.match(result.code, /init_fixture_attached_writer_owner_owner_closure_.*_stage_0/);
  assert.match(result.code, /init_fixture_attached_writer_owner_owner_closure_.*_stage_1/);
  assert.match(result.code, /init_fixture_attached_writer_owner_owner_closure_.*_stage_2/);
  assertRunnableEquivalent({
    prefix: "debundle-owner-closure-attached-writer-pass-",
    source,
    transformedFiles: Object.fromEntries(result.files),
  });
});

test("staged-shell lowering blocks shell statements that eagerly use later extracted owners", () => {
  const source = `function keepRuntime() { return 5; }
const helperSeed = 1;
console.log(readLater() + keepRuntime());
function readLater() { return helperSeed; }
const rendered = readLater() + 1;
console.log(rendered);
`;

  const plan = ownerClosurePlanForCode(source);
  const batchPlan = packOrderedInitOwnerClosures(plan, {
    lowering: "staged_shell",
  });
  const readLaterBatch = batchPlan.candidateBatchPlans.find((candidate) =>
    candidate.seedMemberNames.includes("readLater")
  );
  assert.ok(readLaterBatch);
  assert.match(
    readLaterBatch.blockingReasons.join(","),
    /shell_item_eagerly_uses_later_owner:side_effect_00000:owner_00002/
  );
});

function ownerClosurePlanForCode(source, options) {
  return planOrderedInitOwnerClosureExtractions(
    analyzeRuntimeBoundaryCode(source, {
      chunkId: "static/app",
      runtimePath: "fixture/runtime.js",
      uiVersion: "fixture",
    }),
    options
  );
}

function findOwnerClosurePlanByName(plan, name) {
  const closurePlan = plan.closurePlans.find((candidate) => candidate.seedMemberNames.includes(name));
  if (!closurePlan) {
    throw new Error(`Owner closure plan not found for ${name}`);
  }
  return closurePlan;
}

function findOwnerClosureBatchPlanByName(plan, name) {
  const batchPlan = plan.batchPlans.find((candidate) => candidate.seedMemberNames.includes(name));
  if (!batchPlan) {
    throw new Error(`Owner closure batch plan not found for ${name}`);
  }
  return batchPlan;
}

function assertRunnableEquivalent({ entryFile = "runtime.js", prefix, source, transformedFiles }) {
  const originalDir = createTempFixtureRoot(`${prefix}original-`);
  const transformedDir = createTempFixtureRoot(`${prefix}transformed-`);
  writeRunnableFixture(originalDir, {
    files: {
      [entryFile]: source,
    },
  });
  writeRunnableFixture(transformedDir, {
    files: {
      ...transformedFiles,
    },
  });
  assert.deepEqual(runNodeScript(join(transformedDir, entryFile)), runNodeScript(join(originalDir, entryFile)));
}

function orderedInitOperation(source, { init, ownerNames, targetFile }) {
  const analysis = analyzeRuntimeBoundaryCode(source, {
    chunkId: "static/app",
    runtimePath: "fixture/runtime.js",
    uiVersion: "fixture",
  });
  const ownerIds = ownerNames.map((name) => {
    const owner = analysis.owners.find((candidate) => candidate.names.includes(name));
    if (!owner) {
      throw new Error(`Fixture owner not found for ${name}`);
    }
    return owner.id;
  });
  return {
    id: `extract_${init}`,
    operation: "extract_ordered_init_region",
    selector: {
      chunkId: "static/app",
      file: "runtime.js",
      ownerIds,
    },
    target: {
      file: targetFile,
      init,
    },
  };
}
