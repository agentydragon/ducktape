import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import {
  createWebFixtureRoots,
  FIXTURE_UI_VERSION,
  parseModuleCode,
  readUtf8,
  writeSnapshotFixture,
  makePipelineArtifact,
  makePipelineChunk,
} from "../test_support/fixtures.mjs";
import { createFile, createArtifact } from "../common/artifact.mjs";
import {
  analyzeRuntimeBoundaryCode,
  extractRuntimeBoundaryMetadata,
} from "./boundary.mjs";

test("classifies plain-import, ordered-init, and keep-runtime boundaries", () => {
  const analysis = analyzeRuntimeBoundaryCode(
    `import { ext as dep } from "./dep.js";
const seed = dep + 1;
function plainB() { return dep + 1; }
function plainA() { return plainB(); }
function readsSeed() { return seed; }
let target;
function setTarget(value) { target = value; }
class PureBox { method() { return plainA(); } }
class DerivedBox extends PureBox {
  field = seed;
  static boot = seed;
  [seed]() { return seed; }
}
console.log(target);
`,
    {
      chunkId: "static/fixture",
      runtimePath: "fixture/runtime.js",
      uiVersion: "fixture",
    }
  );

  const ownerByName = new Map(
    analysis.owners.flatMap((owner) => owner.names.map((name) => [name, owner]))
  );

  assert.equal(ownerByName.get("plainA").extractionMode, "plain_import_candidate");
  assert.equal(ownerByName.get("plainB").extractionMode, "plain_import_candidate");
  assert.equal(ownerByName.get("PureBox").extractionMode, "plain_import_candidate");

  assert.equal(ownerByName.get("seed").extractionMode, "ordered_init_candidate");
  assert.match(ownerByName.get("seed").extractionReasons.join(","), /unsupported_plain_import:VariableDeclaration/);

  assert.equal(ownerByName.get("readsSeed").extractionMode, "ordered_init_candidate");
  assert.match(ownerByName.get("readsSeed").extractionReasons.join(","), /blocked_by_non_plain_dependency/);

  assert.equal(ownerByName.get("target").extractionMode, "ordered_init_candidate");
  assert.equal(ownerByName.get("setTarget").extractionMode, "ordered_init_candidate");
  assert.match(ownerByName.get("setTarget").extractionReasons.join(","), /blocked_by_non_plain_dependency/);

  assert.equal(ownerByName.get("DerivedBox").extractionMode, "ordered_init_candidate");
  assert.match(ownerByName.get("DerivedBox").extractionReasons.join(","), /class_has_superclass/);
  assert.match(ownerByName.get("DerivedBox").extractionReasons.join(","), /class_has_static_field_initializer/);
  assert.match(ownerByName.get("DerivedBox").extractionReasons.join(","), /class_has_computed_key/);

  const derivedReads = ownerByName.get("DerivedBox").readsTopLevel;
  assert.ok(derivedReads.eager.some((record) => record.name === "PureBox"));
  assert.ok(derivedReads.eager.some((record) => record.name === "seed"));
  assert.ok(derivedReads.lazy.some((record) => record.name === "seed"));

  const readsSeedReads = ownerByName.get("readsSeed").readsTopLevel;
  assert.deepEqual(
    readsSeedReads.lazy.map((record) => record.name),
    ["seed"]
  );

  assert.equal(analysis.counts.sideEffects, 1);
  assert.equal(analysis.sideEffects[0].type, "ExpressionStatement");
  assert.equal(analysis.sideEffects[0].extractionMode, "keep_runtime");

  assert.ok(
    analysis.graphs.eagerInit.some(
      (edge) =>
        edge.from === ownerByName.get("DerivedBox").id &&
        edge.to === ownerByName.get("seed").id &&
        edge.siteKinds.includes("class_static_field_initializer")
    )
  );
  assert.ok(
    analysis.graphs.mutation.some(
      (edge) =>
        edge.from === ownerByName.get("setTarget").id &&
        edge.to === ownerByName.get("target").id &&
        edge.writeKinds.includes("binding")
    )
  );
});

test("runtime-sensitive syntax fails closed even inside lazy function bodies", () => {
  const analysis = analyzeRuntimeBoundaryCode(
    `function usesMeta(){ return import.meta.url }
function usesEval(){ return eval("1") }
const awaited = await Promise.resolve(1);
`,
    {
      chunkId: "static/runtime-sensitive",
      runtimePath: "fixture/runtime.js",
      uiVersion: "fixture",
    }
  );

  const ownerByName = new Map(
    analysis.owners.flatMap((owner) => owner.names.map((name) => [name, owner]))
  );

  assert.equal(ownerByName.get("usesMeta").extractionMode, "keep_runtime");
  assert.match(ownerByName.get("usesMeta").extractionReasons.join(","), /contains_import_meta/);

  assert.equal(ownerByName.get("usesEval").extractionMode, "keep_runtime");
  assert.match(ownerByName.get("usesEval").extractionReasons.join(","), /contains_direct_eval/);

  assert.equal(ownerByName.get("awaited").extractionMode, "keep_runtime");
  assert.match(ownerByName.get("awaited").extractionReasons.join(","), /contains_top_level_await/);
});

test("root-relative new URL(..., import.meta.url) stays extractable", () => {
  const analysis = analyzeRuntimeBoundaryCode(
    `async function loadWorkerUrl() {
  return new URL("/static/pdf.worker.js", import.meta.url).toString();
}
function loadRelativeAsset() {
  return new URL("./relative.js", import.meta.url).toString();
}
`,
    {
      chunkId: "static/import-meta-root-relative",
      runtimePath: "fixture/runtime.js",
      uiVersion: "fixture",
    }
  );

  const ownerByName = new Map(
    analysis.owners.flatMap((owner) => owner.names.map((name) => [name, owner]))
  );

  assert.equal(ownerByName.get("loadWorkerUrl").effects.containsImportMeta, false);
  assert.equal(ownerByName.get("loadWorkerUrl").extractionMode, "plain_import_candidate");
  assert.doesNotMatch(ownerByName.get("loadWorkerUrl").extractionReasons.join(","), /contains_import_meta/);

  assert.equal(ownerByName.get("loadRelativeAsset").effects.containsImportMeta, true);
  assert.equal(ownerByName.get("loadRelativeAsset").extractionMode, "keep_runtime");
  assert.match(ownerByName.get("loadRelativeAsset").extractionReasons.join(","), /contains_import_meta/);
});

test("variable initializers treat nested async callback bodies as lazy, not top-level await", () => {
  const analysis = analyzeRuntimeBoundaryCode(
    `const shared = 1;
const runner = register(async () => {
  await Promise.resolve();
  return shared;
});
console.log(typeof runner);
`,
    {
      chunkId: "static/nested-variable-callback",
      runtimePath: "fixture/runtime.js",
      uiVersion: "fixture",
    }
  );

  const ownerByName = new Map(
    analysis.owners.flatMap((owner) => owner.names.map((name) => [name, owner]))
  );
  const runner = ownerByName.get("runner");
  assert.ok(runner);
  assert.equal(runner.effects.containsTopLevelAwait, false);
  assert.deepEqual(runner.readsTopLevel.eager.map((record) => record.name), []);
  assert.deepEqual(runner.readsTopLevel.lazy.map((record) => record.name), ["shared"]);
  assert.equal(runner.extractionMode, "ordered_init_candidate");
  assert.doesNotMatch(runner.extractionReasons.join(","), /contains_top_level_await/);
});

test("side-effect statements treat nested handler bodies as lazy accesses", () => {
  const analysis = analyzeRuntimeBoundaryCode(
    `let debugFlag = false;
class CacheTracker {
  static log() {
    return "ok";
  }
}
window.installCacheDebug = () => {
  debugFlag = true;
  return CacheTracker.log();
};
console.log(typeof window.installCacheDebug);
`,
    {
      chunkId: "static/side-effect-handler",
      runtimePath: "fixture/runtime.js",
      uiVersion: "fixture",
    }
  );

  const sideEffect = analysis.sideEffects.find((record) => record.ordinal === 2);
  assert.ok(sideEffect);
  assert.deepEqual(sideEffect.readsTopLevel.eager.map((record) => record.name), []);
  assert.deepEqual(sideEffect.memberWritesTopLevel.eager.map((record) => record.name), []);
  assert.deepEqual(sideEffect.readsTopLevel.lazy.map((record) => record.name), ["CacheTracker"]);
  assert.deepEqual(sideEffect.writesTopLevel.lazy.map((record) => record.name), ["debugFlag"]);
});

test("reports contiguous ordered-init regions and their outside-local blockers", () => {
  const analysis = analyzeRuntimeBoundaryCode(
    `const outside = 1;
function helper() { return outside; }

const unrelated = 2;
function unrelatedFn() { return unrelated; }

let state = 0;
function inside() { state += 1; return helper() + state; }
function inside2() { return inside(); }

let localOnly = 0;
function bumpLocalOnly() { localOnly += 1; return localOnly; }
function readLocalOnly() { return bumpLocalOnly(); }
`,
    {
      chunkId: "static/ordered-init-regions",
      runtimePath: "fixture/runtime.js",
      uiVersion: "fixture",
    }
  );

  const blockedRegion = analysis.orderedInitRegions.find((region) => region.memberNames.includes("inside"));
  assert.ok(blockedRegion);
  assert.equal(blockedRegion.orderedInitExtractable, false);
  assert.ok(blockedRegion.memberNames.includes("inside2"));
  assert.ok(blockedRegion.memberNames.includes("state"));
  assert.deepEqual(blockedRegion.outsideLocalDependencyNames, ["helper"]);
  assert.match(
    blockedRegion.orderedInitBlockingReasons.join(","),
    /depends_on_outside_local_owner:/
  );

  const extractableRegion = analysis.orderedInitRegions.find((region) => region.memberNames.includes("bumpLocalOnly"));
  assert.ok(extractableRegion);
  assert.equal(extractableRegion.orderedInitExtractable, true);
  assert.deepEqual(extractableRegion.outsideLocalDependencyOwnerIds, []);
  assert.deepEqual(extractableRegion.orderedInitBlockingReasons, []);
});

test("extractRuntimeBoundaryMetadata works without precomputed manifests and with no emitted parts", () => {
  const { analysisRoot, extractedRoot, snapshotRoot } = createWebFixtureRoots("debundle-boundary-no-manifest-");
  writeSnapshotFixture({
    assetSummary: {
      entryPoints: {
        html: "index.html",
        js: ["static/app.js"],
      },
      uiVersion: FIXTURE_UI_VERSION,
    },
    extractedRoot,
    files: {
      "static/app.js": `const seed = 1;
function readSeed() { return seed; }
console.log(readSeed());
export { readSeed };
`,
    },
    html: `<!doctype html><html><body><script type="module" src="/static/app.js"></script></body></html>\n`,
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const artifact = createArtifact({
    chunks: [
      {
        chunkId: "static/app",
        entryFile: "runtime.js",
        files: [
          createFile({
            path: "runtime.js",
            ast: parseModuleCode(readUtf8(join(snapshotRoot, "static", "app.js"))),
            metadata: {
              chunkId: "static/app",
              chunkFile: "runtime.js",
              role: "entry",
            },
          }),
        ],
      },
    ],
  });

  const { manifest } = extractRuntimeBoundaryMetadata({
    artifact,
    force: true,
    inputRoot: snapshotRoot,
    outDir: analysisRoot,
  });

  assert.equal(manifest.chunks.length, 1);
  assert.equal(manifest.chunks[0].chunkId, "static/app");
  assert.ok(existsSync(join(analysisRoot, "static", "app.json")));
});

test("marks class-style singleton regions extractable when their closure is self-contained", () => {
  const analysis = analyzeRuntimeBoundaryCode(
    `import { now } from "./clock.js";
class DeferredRenderCounter {
  constructor() {
    this.startedAt = now();
    this.count = 0;
  }
  bump() {
    this.count += 1;
    return this.count;
  }
}
const counter = new DeferredRenderCounter();
const first = counter.bump();
console.log(first);
`,
    {
      chunkId: "static/class-singleton",
      runtimePath: "fixture/runtime.js",
      uiVersion: "fixture",
    }
  );

  const region = analysis.orderedInitRegions.find((candidate) =>
    candidate.memberNames.includes("DeferredRenderCounter")
  );
  assert.ok(region);
  assert.equal(region.orderedInitExtractable, true);
  assert.deepEqual(
    region.memberNames,
    ["DeferredRenderCounter", "counter", "first"]
  );
  assert.deepEqual(region.outsideLocalDependencyNames, []);
});

test("reports drag-selection style lazy helper dependencies as blockers", () => {
  const analysis = analyzeRuntimeBoundaryCode(
    `const dragSelectionState = { origin: 2 };
function readDragSelectionOrigin() {
  return dragSelectionState.origin;
}
const unrelated = 1;
function unrelatedFn() {
  return unrelated;
}
class DragSelectionSession {
  current() {
    return readDragSelectionOrigin();
  }
}
const dragSelectionSession = new DragSelectionSession();
console.log(dragSelectionSession.current(), unrelatedFn());
`,
    {
      chunkId: "static/drag-selection",
      runtimePath: "fixture/runtime.js",
      uiVersion: "fixture",
    }
  );

  const region = analysis.orderedInitRegions.find((candidate) =>
    candidate.memberNames.includes("DragSelectionSession")
  );
  assert.ok(region);
  assert.equal(region.orderedInitExtractable, false);
  assert.deepEqual(region.outsideLocalDependencyNames, ["readDragSelectionOrigin"]);
  assert.match(region.orderedInitBlockingReasons.join(","), /depends_on_outside_local_owner:/);
});

test("file-mode extraction writes per-chunk reports and summary", () => {
  const { analysisRoot, transformedRoot: inputRoot } = createWebFixtureRoots("debundle-runtime-boundary-analysis-");
  const outDir = join(analysisRoot, "boundaries");
  const summaryPath = join(analysisRoot, "boundary-summary.json");
  const artifact = makePipelineArtifact([
    makePipelineChunk("static/a", {
      "runtime.js": `const value = 1; function read(){ return value; }\n`,
    }),
  ]);

  const { manifest: summary } = extractRuntimeBoundaryMetadata({
    artifact,
    force: true,
    inputRoot,
    outDir,
    summaryPath,
    inputManifestPath: join(inputRoot, "manifest.json"),
  });

  assert.equal(summary.kind, "js.runtime_boundary_summary");
  assert.equal(summary.counts.chunks, 1);
  assert.ok(existsSync(summaryPath));
  assert.ok(existsSync(join(outDir, "static", "a.json")));

  const chunkReport = JSON.parse(readUtf8(join(outDir, "static", "a.json")));
  assert.equal(chunkReport.chunkId, "static/a");
  assert.equal(chunkReport.counts.declarationOwners, 2);
  assert.equal(chunkReport.owners.some((owner) => owner.names.includes("read")), true);
});
