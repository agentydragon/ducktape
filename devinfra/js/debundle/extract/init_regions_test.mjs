import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import { analyzeRuntimeBoundaryCode } from "../analysis/boundary.mjs";
import { getArtifactChunkManifest, getChunkEntryFile } from "../common/artifact.mjs";
import { createTempFixtureRoot, makePipelineArtifact, makePipelineChunk, runNodeScript, writeRunnableFixture } from "../test_support/fixtures.mjs";
import { extractOrderedInitRegions } from "./init_regions.mjs";

const SOURCE = `const seed = 1;
function render() { return seed + 1; }
console.log(render());
export { render as publicRender };
`;

function findFixtureOwnerIds(source, chunkId) {
  const analysis = analyzeRuntimeBoundaryCode(source, {
    chunkId,
    runtimePath: "fixture/entry.js",
    uiVersion: "fixture",
  });
  return ["seed", "render"].map((name) => {
    const owner = analysis.owners.find((candidate) => candidate.names.includes(name));
    if (!owner) {
      throw new Error(`Missing fixture owner ${name}`);
    }
    return owner.id;
  });
}

function makeEntryChunk(chunkId, source = SOURCE, manifest = {}) {
  return makePipelineChunk(
    chunkId,
    {
      "entry.js": source,
    },
    {
      manifest: {
        entryFile: "entry.js",
        ...manifest,
      },
    }
  );
}

function orderedInitExtractionRecord({ chunkId, file = "entry.js", id, targetFile }) {
  return {
    chunkId,
    exportedNames: [],
    file,
    id,
    init: `init_${id}`,
    operation: "extract_ordered_init_region",
    ownerIds: [`owner_${id}`],
    targetFile,
  };
}

test("extractOrderedInitRegions resolves a missing selector.file from the chunk entryFile", () => {
  const source = SOURCE;
  const ownerIds = findFixtureOwnerIds(source, "static/app");

  const artifact = makePipelineArtifact([
    makeEntryChunk("static/app", source),
  ]);
  const outDir = createTempFixtureRoot("debundle-extract-snapshot-entry-file-");

  const result = extractOrderedInitRegions({
    artifact,
    force: true,
    operations: [
      {
        id: "extract_entry_region",
        operation: "extract_ordered_init_region",
        selector: {
          chunkId: "static/app",
          ownerIds,
        },
        target: {
          file: "regions/entry_region.js",
          init: "init_entry_region",
        },
      },
    ],
    outDir,
  });

  const entryFile = getChunkEntryFile(result.artifact, "static/app");
  assert.equal(entryFile?.metadata?.chunkFile, "entry.js");
  assert.match(entryFile?.ast ? readFileSync(join(outDir, "static", "app", "entry.js"), "utf8") : "", /init_entry_region/);
  assert.equal(existsSync(join(outDir, "static", "app", "entry.js")), true);
  assert.equal(existsSync(join(outDir, "static", "app", "regions", "entry_region.js")), true);

  const originalDir = createTempFixtureRoot("debundle-extract-snapshot-entry-file-original-");
  writeRunnableFixture(originalDir, {
    files: {
      "entry.js": source,
    },
  });
  assert.deepEqual(runNodeScript(join(outDir, "static", "app", "entry.js")), runNodeScript(join(originalDir, "entry.js")));
});

test("extractOrderedInitRegions can update the artifact without writing files", () => {
  const source = SOURCE;
  const ownerIds = findFixtureOwnerIds(source, "static/app");

  const artifact = makePipelineArtifact([
    makeEntryChunk("static/app", source),
  ]);
  const outDir = createTempFixtureRoot("debundle-extract-snapshot-entry-file-no-write-");

  const result = extractOrderedInitRegions({
    artifact,
    force: true,
    operations: [
      {
        id: "extract_entry_region",
        operation: "extract_ordered_init_region",
        selector: {
          chunkId: "static/app",
          ownerIds,
        },
        target: {
          file: "regions/entry_region.js",
          init: "init_entry_region",
        },
      },
    ],
    outDir,
    write: false,
  });

  const entryFile = getChunkEntryFile(result.artifact, "static/app");
  assert.equal(entryFile?.metadata?.chunkFile, "entry.js");
  assert.equal(existsSync(join(outDir, "static", "app", "entry.js")), false);
  assert.equal(existsSync(join(outDir, "static", "app", "regions", "entry_region.js")), false);
  assert.match(result.manifest.outDir, /debundle-extract-snapshot-entry-file-no-write-/);
});

test("extractOrderedInitRegions merges per-chunk extraction manifests without cross-chunk leakage", () => {
  const appChunkId = "static/app";
  const dashboardChunkId = "static/dashboard";
  const appOwnerIds = findFixtureOwnerIds(SOURCE, appChunkId);
  const dashboardOwnerIds = findFixtureOwnerIds(SOURCE, dashboardChunkId);
  const artifact = makePipelineArtifact(
    [
      makeEntryChunk(appChunkId, SOURCE, {
        orderedInitExtractions: [
          orderedInitExtractionRecord({
            chunkId: appChunkId,
            id: "existing_app_region",
            targetFile: "regions/existing_app_region.js",
          }),
        ],
      }),
      makeEntryChunk(dashboardChunkId, SOURCE, {
        orderedInitExtractions: [
          orderedInitExtractionRecord({
            chunkId: dashboardChunkId,
            id: "existing_dashboard_region",
            targetFile: "regions/existing_dashboard_region.js",
          }),
        ],
      }),
    ],
    {
      manifest: {
        counts: {
          chunks: 2,
          orderedInitExtractions: 1,
        },
        orderedInitExtractions: [
          orderedInitExtractionRecord({
            chunkId: "static/root",
            id: "existing_root_region",
            targetFile: "regions/existing_root_region.js",
          }),
        ],
      },
    }
  );
  const outDir = createTempFixtureRoot("debundle-extract-snapshot-multi-chunk-");

  const result = extractOrderedInitRegions({
    artifact,
    force: true,
    operations: [
      {
        id: "extract_app_region",
        operation: "extract_ordered_init_region",
        selector: {
          chunkId: appChunkId,
          ownerIds: appOwnerIds,
        },
        target: {
          file: "regions/app_region.js",
          init: "init_app_region",
        },
      },
      {
        id: "extract_dashboard_region",
        operation: "extract_ordered_init_region",
        selector: {
          chunkId: dashboardChunkId,
          ownerIds: dashboardOwnerIds,
        },
        target: {
          file: "regions/dashboard_region.js",
          init: "init_dashboard_region",
        },
      },
    ],
    outDir,
    write: false,
  });

  assert.deepEqual(
    getArtifactChunkManifest(result.artifact, appChunkId)?.orderedInitExtractions.map((record) => record.id),
    ["existing_app_region", "extract_app_region"]
  );
  assert.deepEqual(
    getArtifactChunkManifest(result.artifact, dashboardChunkId)?.orderedInitExtractions.map((record) => record.id),
    ["existing_dashboard_region", "extract_dashboard_region"]
  );
  assert.deepEqual(
    result.manifest.orderedInitExtractions.map((record) => record.id),
    ["existing_root_region", "extract_app_region", "extract_dashboard_region"]
  );
  assert.equal(result.manifest.counts.orderedInitExtractions, 3);
});
