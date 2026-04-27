import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import { writeTextFile } from "../common/parser_options.mjs";
import { createFile, createArtifact } from "../common/artifact.mjs";
import { makePipelineArtifact, makePipelineChunk, parseModuleCode, readUtf8 } from "../test_support/fixtures.mjs";
import {
  createWebFixtureRoots,
  FIXTURE_UI_VERSION,
  writeChunkFixture,
  writeSnapshotManifest,
} from "../test_support/fixtures.mjs";
import {
  extractScrambledIdentifierFrequencies,
  isScrambledIdentifier,
} from "./identifier_frequency.mjs";

function writeScrambledIdentifierFixture(prefix) {
  const { analysisRoot: outDir, root, transformedRoot: inputRoot } = createWebFixtureRoots(prefix);
  const appChunkDir = join(inputRoot, "static", "app");
  const vendorChunkDir = join(inputRoot, "static", "vendor");

  writeTextFile(join(outDir, "keep.txt"), "keep\n");
  writeSnapshotManifest(inputRoot, {
    chunks: [
      {
        chunkId: "static/app",
        counts: {
          parts: 1,
        },
        inputPath: "snapshot/static/app.js",
      },
      {
        chunkId: "static/vendor",
        counts: {
          parts: 0,
        },
        inputPath: "snapshot/static/vendor.js",
      },
    ],
    renames: [
      {
        id: "rename_react_fixture",
        to: "React",
      },
    ],
  });
  writeChunkFixture({
    chunkId: "static/app",
    manifest: {
      parts: [
        {
          file: "parts/part-0001.js",
        },
      ],
    },
    parts: {
      "parts/part-0001.js": `export function qGt(e) {
  const _x = e + 1;
  return _x;
}
`,
    },
    root: inputRoot,
    runtime: `import { x as zz } from "../vendor/runtime.js";
class ReadableClass {
  value(e, t) {
    const r = e + t;
    return r + this.clearName;
  }
}
function qGt() {
  return 1;
}
const React = {};
console.log(new ReadableClass().value(1, 2));
console.log(qGt(), React, zz());
export { ReadableClass, qGt };
`,
  });
  writeChunkFixture({
    chunkId: "static/vendor",
    manifest: {
      parts: [],
    },
    root: inputRoot,
    runtime: `function x() {
  return 1;
}
export { x };
`,
  });

  return {
    appChunkDir,
    inputRoot,
    outDir,
    root,
    vendorChunkDir,
  };
}

function buildFixtureArtifact({ appChunkDir, inputRoot, vendorChunkDir }, { withVendorAnnotation = true } = {}) {
  return makePipelineArtifact(
    [
      makePipelineChunk(
        "static/app",
        {
          "runtime.js": {
            ast: parseModuleCode(readUtf8(join(appChunkDir, "runtime.js"))),
          },
          "parts/part-0001.js": {
            ast: parseModuleCode(readUtf8(join(appChunkDir, "parts", "part-0001.js"))),
          },
        },
        {
          manifest: {
            chunkId: "static/app",
            parts: [{ file: "parts/part-0001.js" }],
          },
        }
      ),
      makePipelineChunk(
        "static/vendor",
        {
          "runtime.js": {
            ast: parseModuleCode(readUtf8(join(vendorChunkDir, "runtime.js"))),
          },
        },
        {
          manifest: {
            chunkId: "static/vendor",
            parts: [],
          },
        }
      ),
    ],
    {
      annotations: withVendorAnnotation
        ? {
            vendor: new Map([
              [
                "static/vendor",
                {
                  id: "mark_vendor_fixture",
                  chunkId: "static/vendor",
                  chunkPath: "static/vendor.js",
                  identity: "fixture-vendor",
                  level: "suppress",
                  evidence: [{ path: "static/vendor.js", text: "fixture" }],
                },
              ],
            ]),
          }
        : undefined,
      manifest: JSON.parse(readUtf8(join(inputRoot, "manifest.json"))),
    }
  );
}

test("isScrambledIdentifier distinguishes minified top-level names from readable ones", () => {
  assert.equal(isScrambledIdentifier("e"), true);
  assert.equal(isScrambledIdentifier("qGt"), true);
  assert.equal(isScrambledIdentifier("_x"), true);
  assert.equal(isScrambledIdentifier("ReadableClass"), false);
  assert.equal(isScrambledIdentifier("console"), false);
  assert.equal(isScrambledIdentifier("React"), true);
});

test("extractScrambledIdentifierFrequencies writes binding-resolved report without clearing sibling files", () => {
  const fixture = writeScrambledIdentifierFixture("debundle-scrambled-identifiers-");
  const artifact = buildFixtureArtifact(fixture);

  const { manifest: report } = extractScrambledIdentifierFrequencies({
    artifact,
    excludedSymbolFiles: ["static/vendor.js"],
    force: true,
    inputRoot: fixture.inputRoot,
    inputManifestPath: join(fixture.inputRoot, "manifest.json"),
    limit: 20,
    outDir: fixture.outDir,
  });

  assert.equal(report.counts.files, 2);
  assert.ok(existsSync(join(fixture.outDir, "scrambled-identifiers.json")));
  assert.equal(existsSync(join(fixture.outDir, "scrambled-identifiers.md")), false);
  assert.equal(readUtf8(join(fixture.outDir, "keep.txt")), "keep\n");
  assert.deepEqual(report.heuristic.renameTargetIdentifiers, ["React"]);
  assert.equal("identifiers" in report, false);
  assert.equal("topLevelIdentifiers" in report, false);

  assert.equal(report.counts.topLevelScrambledSymbols, report.symbols.length);
  assert.equal(report.symbols[0].references >= report.symbols[1].references, true);
  assert.equal("occurrences" in report.symbols[0], false);
  assert.equal("mentions" in report.symbols[0].topFiles[0], true);

  const qGtSymbols = report.symbols.filter((symbol) => symbol.name === "qGt");
  assert.equal(qGtSymbols.length, 2);
  assert.deepEqual(qGtSymbols.map((symbol) => symbol.declaration.file).sort(), [
    "static/app/parts/part-0001.js",
    "static/app/runtime.js",
  ]);
  assert.ok(qGtSymbols.every((symbol) => symbol.id.includes(":FunctionDeclaration:qGt")));

  const partQgTSymbol = qGtSymbols.find((symbol) => symbol.declaration.file === "static/app/parts/part-0001.js");
  const runtimeQgTSymbol = qGtSymbols.find((symbol) => symbol.declaration.file === "static/app/runtime.js");
  assert.ok((partQgTSymbol?.bindings ?? 0) >= 1);
  assert.equal(partQgTSymbol?.references, 0);
  assert.ok((runtimeQgTSymbol?.bindings ?? 0) >= 1);
  assert.ok((runtimeQgTSymbol?.references ?? 0) > (partQgTSymbol?.references ?? 0));

  const zzSymbol = report.symbols.find((symbol) => symbol.name === "zz");
  assert.ok(zzSymbol);
  assert.equal(zzSymbol.declaration.kind, "ImportSpecifier");
  assert.equal(zzSymbol.declaration.import?.source, "../vendor/runtime.js");
  assert.equal(zzSymbol.declaration.import?.imported, "x");
  assert.equal(zzSymbol.bindings, 1);
  assert.equal(zzSymbol.references, 1);

  assert.equal(report.symbols.some((symbol) => symbol.name === "_x"), false);
  assert.equal(report.symbols.some((symbol) => symbol.name === "e"), false);
  assert.equal(report.symbols.some((symbol) => symbol.name === "x"), false);

  const persisted = JSON.parse(readUtf8(join(fixture.outDir, "scrambled-identifiers.json")));
  assert.equal(persisted.symbols[0].id, report.symbols[0].id);
});

test("extractScrambledIdentifierFrequencies merges explicit and annotated exclusions", () => {
  const fixture = writeScrambledIdentifierFixture("debundle-scrambled-identifiers-artifact-");

  const artifact = buildFixtureArtifact(fixture, { withVendorAnnotation: true });
  const annotationOutDir = join(fixture.root, "analysis-annotations");
  const { manifest: reportFromAnnotation } = extractScrambledIdentifierFrequencies({
    artifact,
    inputRoot: fixture.inputRoot,
    inputManifestPath: join(fixture.inputRoot, "manifest.json"),
    limit: 20,
    outDir: annotationOutDir,
    force: true,
  });
  assert.deepEqual(reportFromAnnotation.heuristic.excludedSymbolFiles, ["static/vendor.js"]);
  assert.equal(reportFromAnnotation.counts.files, 2);
  assert.equal(
    reportFromAnnotation.symbols.some((symbol) => symbol.declaration.chunkId === "static/vendor"),
    false
  );

  const noVendorArtifact = buildFixtureArtifact(fixture, { withVendorAnnotation: false });
  const noAnnotationOutDir = join(fixture.root, "analysis-no-annotations");
  const { manifest: reportWithoutAnnotation } = extractScrambledIdentifierFrequencies({
    artifact: noVendorArtifact,
    inputRoot: fixture.inputRoot,
    inputManifestPath: join(fixture.inputRoot, "manifest.json"),
    limit: 20,
    outDir: noAnnotationOutDir,
    force: true,
  });
  assert.deepEqual(reportWithoutAnnotation.heuristic.excludedSymbolFiles, []);
  assert.equal(reportWithoutAnnotation.counts.files, 3);

  const unionOutDir = join(fixture.root, "analysis-union");
  const { manifest: reportUnion } = extractScrambledIdentifierFrequencies({
    artifact,
    excludedSymbolFiles: ["static/app.js"],
    inputRoot: fixture.inputRoot,
    inputManifestPath: join(fixture.inputRoot, "manifest.json"),
    limit: 20,
    outDir: unionOutDir,
    force: true,
  });
  assert.deepEqual(reportUnion.heuristic.excludedSymbolFiles, ["static/app.js", "static/vendor.js"]);
  assert.equal(reportUnion.counts.files, 0);
});

test("extractScrambledIdentifierFrequencies works without manifests and with no emitted parts", () => {
  const { analysisRoot: outDir, root } = createWebFixtureRoots("debundle-scrambled-no-manifest-");
  const artifact = createArtifact({
    chunks: [
      {
        chunkId: "static/app",
        entryFile: "runtime.js",
        files: [
          createFile({
            path: "runtime.js",
            ast: parseModuleCode(`const qGt = 1; console.log(qGt); export { qGt };\n`),
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

  const { manifest: report } = extractScrambledIdentifierFrequencies({
    artifact,
    force: true,
    inputRoot: root,
    limit: 20,
    outDir,
  });

  assert.equal(report.counts.files, 1);
  assert.equal(report.symbols.some((symbol) => symbol.name === "qGt"), true);
  assert.ok(existsSync(join(outDir, "scrambled-identifiers.json")));
});
