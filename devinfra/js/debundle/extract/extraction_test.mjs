import assert from "node:assert/strict";
import { join } from "node:path";
import test from "node:test";
import { parse } from "@babel/parser";
import { analyzeRuntimeBoundaryAst, analyzeRuntimeBoundaryCode } from "../analysis/boundary.mjs";
import { computeJsAsts } from "../common/parse_asts.mjs";
import { getChunkEntryFile } from "../common/artifact.mjs";
import { DEFAULT_PARSER_OPTIONS } from "../common/parser_options.mjs";
import { loadJsChunks } from "../common/load_chunks.mjs";
import { serializeGeneratedJsFile } from "../split/chunk.mjs";
import { normalizeJsChunks } from "../common/normalize.mjs";
import { createTempFixtureRoot, createWebFixtureRoots, runNodeScript, writeRunnableFixture, writeSnapshotFixture } from "../test_support/fixtures.mjs";
import { writeJsTree } from "../transforms/write.mjs";
import { extractGuidedSelectedOwnerModulesInAst } from "./init_region.mjs";
import { planGuidedSelectedOwnerModules, planSelectedAtomicModules } from "./planner.mjs";
import { extractGuidedSelectedOwnerModules } from "./packed_modules.mjs";

test("planGuidedSelectedOwnerModules merges legal atomic units into size-guided modules", () => {
  const source = fixtureSource();
  const ast = parse(source, DEFAULT_PARSER_OPTIONS);
  const analysis = analyzeRuntimeBoundaryCode(source, {
    ast,
    chunkId: "static/app",
    runtimePath: "fixture/runtime.js",
    uiVersion: "fixture",
  });

  const plan = planGuidedSelectedOwnerModules(
    {
      analysis,
      code: source,
      programBody: ast.program.body,
    },
    {
      minModuleLines: 4,
      maxModuleLines: 20,
    }
  );

  assert.equal(plan.kind, "js.guided_selected_owner_module_plan");
  assert.ok(plan.atomicUnitCount > plan.modulePlans.length);
  assert.ok(plan.modulePlans.length > 1);
  for (const modulePlan of plan.modulePlans) {
    assert.ok(modulePlan.lines <= 20);
  }
  for (const modulePlan of plan.modulePlans.slice(0, -1)) {
    assert.ok(modulePlan.lines >= 4);
  }
});

test("planGuidedSelectedOwnerModules can pack from compact item metrics without program AST", () => {
  const source = fixtureSource();
  const ast = parse(source, DEFAULT_PARSER_OPTIONS);
  const analysis = analyzeRuntimeBoundaryCode(source, {
    ast,
    chunkId: "static/app",
    runtimePath: "fixture/runtime.js",
    uiVersion: "fixture",
  });
  const itemMetricsById = new Map(
    analysis.programItems.map((item) => {
      const statement = ast.program.body[item.ordinal];
      return [
        item.id,
        {
          bytes:
            typeof statement?.start === "number" && typeof statement?.end === "number"
              ? Buffer.byteLength(source.slice(statement.start, statement.end))
              : 0,
          lines:
            statement?.loc
              ? statement.loc.end.line - statement.loc.start.line + 1
              : 0,
        },
      ];
    })
  );

  const fromProgramBody = planGuidedSelectedOwnerModules(
    {
      analysis,
      code: source,
      programBody: ast.program.body,
    },
    {
      minModuleLines: 4,
      maxModuleLines: 20,
    }
  );
  const fromMetricsOnly = planGuidedSelectedOwnerModules(
    {
      analysis,
      code: source,
      itemMetricsById,
    },
    {
      minModuleLines: 4,
      maxModuleLines: 20,
    }
  );

  assert.deepEqual(
    fromMetricsOnly.modulePlans.map((modulePlan) => ({
      attachedItemIds: modulePlan.attachedItemIds,
      bytes: modulePlan.bytes,
      lines: modulePlan.lines,
      ownerIds: modulePlan.ownerIds,
      unitIds: modulePlan.unitIds,
    })),
    fromProgramBody.modulePlans.map((modulePlan) => ({
      attachedItemIds: modulePlan.attachedItemIds,
      bytes: modulePlan.bytes,
      lines: modulePlan.lines,
      ownerIds: modulePlan.ownerIds,
      unitIds: modulePlan.unitIds,
    }))
  );
});

test("planSelectedAtomicModules rejects unknown selected owner ids that appear in access edges", () => {
  const analysis = {
    owners: [
      {
        id: "owner_known",
        memberWritesTopLevel: { eager: [], lazy: [] },
        names: ["KnownOwner"],
        ordinal: 0,
        readsTopLevel: { eager: [], lazy: [] },
        type: "VariableDeclaration",
        writesTopLevel: {
          eager: [{ kind: "local_declaration", ownerId: "owner_missing" }],
          lazy: [],
        },
      },
    ],
    programItems: [{ id: "owner_known", ordinal: 0 }],
    sideEffects: [],
  };

  assert.throws(
    () =>
      planSelectedAtomicModules(
        {
          analysis,
          code: "const KnownOwner = 1;",
          itemMetricsById: new Map([
            [
              "owner_known",
              {
                bytes: 21,
                lines: 1,
              },
            ],
          ]),
        },
        {
          selectedOwnerIds: ["owner_known", "owner_missing"],
        }
      ),
    /unknown owner ids outside analysis\.owners: owner_missing/
  );
});

test("guided selected owner modules lower in one pass and preserve behavior", () => {
  const source = fixtureSource();
  const ast = parse(source, DEFAULT_PARSER_OPTIONS);
  const analysis = analyzeRuntimeBoundaryCode(source, {
    ast,
    chunkId: "static/app",
    runtimePath: "fixture/runtime.js",
    uiVersion: "fixture",
  });
  const plan = planGuidedSelectedOwnerModules(
    {
      analysis,
      code: source,
      programBody: ast.program.body,
    },
    {
      minModuleLines: 4,
      maxModuleLines: 20,
    }
  );
  const result = extractGuidedSelectedOwnerModulesInAst(ast, plan, {
    analysis,
    chunkId: "static/app",
    file: "runtime.js",
    filePrefix: "guided_",
    headerLines: [],
    idPrefix: "guided_fixture",
    initPrefix: "init_guided_",
    targetDir: "regions",
  });

  const transformedFiles = new Map(
    [...result.jsFiles.entries()].map(([relativePath, fileArtifact]) => [relativePath, serializeGeneratedJsFile(fileArtifact)])
  );
  const extractedFiles = [...transformedFiles.keys()].filter((file) => file.startsWith("regions/guided_"));
  assert.ok(extractedFiles.length > 1);
  const extractedSources = extractedFiles.map((file) => transformedFiles.get(file));
  assert.ok(extractedSources.some((code) => /from "\.\/guided_/.test(code)));
  for (const extractedSource of extractedSources) {
    assert.doesNotThrow(() => parse(extractedSource, DEFAULT_PARSER_OPTIONS));
  }

  assertRunnableEquivalent({
    prefix: "debundle-guided-selected-owner-modules-",
    source,
    transformedFiles: Object.fromEntries(transformedFiles),
  });
});

test("guided selected owner stage keeps mixed runtime side effects in the spine", async () => {
  const { extractedRoot, outRoot, snapshotRoot } = createWebFixtureRoots(
    "debundle-guided-selected-owner-stage-mixed-runtime-"
  );
  const source = `const runtimeOnly = 7;
const extractedValue = 42;
console.log(JSON.stringify({ extractedValue, runtimeOnly }));
export { extractedValue };
`;

  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/app.js": source,
    },
    jsFiles: ["static/app.js"],
    snapshotRoot,
  });

  const loaded = loadJsChunks({
    inputRoot: snapshotRoot,
    jsListPath: join(extractedRoot, "js-files.txt"),
  });
  const parsed = computeJsAsts({
    artifact: loaded.artifact,
  });
  const normalized = await normalizeJsChunks({
    artifact: parsed.artifact,
    jobs: 1,
  });
  const runtimeFile = getChunkEntryFile(normalized.artifact, "static/app");
  const analysis = analyzeRuntimeBoundaryAst(runtimeFile.ast, {
    chunkId: "static/app",
    manifestPath: "static/app/manifest.json",
    runtimePath: "static/app/entry.js",
    uiVersion: "fixture",
  });
  const selectedOwnerId = analysis.owners.find((owner) => owner.names.includes("extractedValue"))?.id;
  assert.ok(selectedOwnerId);

  const result = extractGuidedSelectedOwnerModules({
    artifact: normalized.artifact,
    chunkIds: ["static/app"],
    filePrefix: "guided_",
    idPrefix: "guided_fixture",
    initPrefix: "init_guided_",
    maxModuleLines: 1000,
    minModuleLines: 1,
    pruneOtherChunks: false,
    selectedOwnerIdsByChunk: {
      "static/app": [selectedOwnerId],
    },
    targetDir: "modules",
  });

  writeJsTree({
    artifact: result.artifact,
    force: true,
    outDir: outRoot,
  });

  assert.equal(result.manifest.chunks[0]?.modules, 1);
  assert.deepEqual(runNodeScript(join(outRoot, "static", "app", "entry.js")), runNodeScript(join(snapshotRoot, "static", "app.js")));
});

function fixtureSource() {
  return `const seed = 1;
function readSeed() {
  return seed;
}

console.log("guided-barrier-0");

const first = readSeed() + 1;
function readFirst() {
  return first;
}

console.log("guided-barrier-1");

var Status = ((Status2) => {
  Status2.Ready = "ready";
  Status2.Done = "done";
  return Status2;
})(Status || {});

const second = readFirst() + 1;
function render() {
  return \`\${Status.Done}:\${second}\`;
}

console.log(JSON.stringify({
  seed: readSeed(),
  first: readFirst(),
  value: render(),
}));

export { render as publicRender };
`;
}

function assertRunnableEquivalent({ prefix, source, transformedFiles }) {
  const originalDir = createTempFixtureRoot(`${prefix}original-`);
  const transformedDir = createTempFixtureRoot(`${prefix}transformed-`);
  writeRunnableFixture(originalDir, {
    files: {
      "runtime.js": source,
    },
  });
  writeRunnableFixture(transformedDir, {
    files: {
      ...transformedFiles,
    },
  });
  assert.deepEqual(runNodeScript(join(transformedDir, "runtime.js")), runNodeScript(join(originalDir, "runtime.js")));
}
