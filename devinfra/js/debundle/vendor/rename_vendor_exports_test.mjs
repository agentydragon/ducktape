import assert from "node:assert/strict";
import test from "node:test";
import generateModule from "@babel/generator";
import { getChunkEntryPath, requireChunkFile } from "../common/pipeline_artifact_lib.mjs";
import { makePipelineArtifact, makePipelineChunk } from "../test_support/fixture_lib.mjs";
import { renameVendorExports } from "./rename_vendor_exports_lib.mjs";

const generate = generateModule.default ?? generateModule;

function genCode(ast) {
  return generate(ast, { comments: false }).code;
}

function chunkCode(artifact, chunkId) {
  const entryFile = getChunkEntryPath(artifact, chunkId);
  if (!entryFile) {
    throw new Error(`Missing entry file for chunk ${chunkId}`);
  }
  return genCode(requireChunkFile(artifact, chunkId, entryFile, "renameVendorExportsTest").ast);
}

function makeChunk(chunkId, files, options) {
  return makePipelineChunk(chunkId, files, options);
}

function makeArtifact(chunks) {
  return makePipelineArtifact(chunks);
}

function vendorOp(overrides = {}) {
  return {
    id: "op_katex",
    operation: "mark_vendor",
    level: "boundary-rename",
    chunkPath: "static/katex-BZy9Y_85.js",
    identity: "katex/dist/katex.mjs",
    evidence: [{ path: "x", line: 1, text: "y" }],
    ...overrides,
  };
}

test("happy path: scrambled import rewritten to real upstream name", () => {
  const artifact = makeArtifact([
    makeChunk("static/katex-BZy9Y_85", {
      "runtime.js": `const ua = () => {};\nexport { ua as render };\n`,
    }),
    makeChunk("static/index-AppEntry", {
      "runtime.js": `import { ua as r } from "../katex-BZy9Y_85/runtime.js";\nr();\n`,
    }),
  ]);

  const { manifest } = renameVendorExports({
    artifact,
    operations: [vendorOp()],
  });
  assert.equal(manifest.counts.considered, 1);
  assert.equal(manifest.counts.chunksWithMapping, 1);
  assert.equal(manifest.counts.rewrites, 1);
  assert.match(chunkCode(artifact, "static/index-AppEntry"), /import\s*\{\s*render as r\s*\}\s*from\s*"\.\.\/katex-BZy9Y_85\/runtime\.js"/);
});

test("source-path imports are rewritten before late chunk-entry realization", () => {
  const artifact = makeArtifact([
    makeChunk(
      "static/katex-BZy9Y_85",
      {
        "entry.js": `const ua = () => {};\nexport { ua as render };\n`,
      },
      {
        manifest: {
          sourcePath: "static/katex-BZy9Y_85.js",
        },
      }
    ),
    makeChunk(
      "static/index-AppEntry",
      {
        "entry.js": `import { ua as r } from "./katex-BZy9Y_85.js";\nr();\n`,
      },
      {
        manifest: {
          sourcePath: "static/index-AppEntry.js",
        },
      }
    ),
  ]);

  const { manifest } = renameVendorExports({
    artifact,
    operations: [vendorOp()],
  });

  assert.equal(manifest.counts.rewrites, 1);
  assert.match(
    chunkCode(artifact, "static/index-AppEntry"),
    /import\s*\{\s*render as r\s*\}\s*from\s*"\.\/katex-BZy9Y_85\.js"/
  );
});

test("already aligned consumer is a no-op", () => {
  const artifact = makeArtifact([
    makeChunk("static/katex-BZy9Y_85", {
      "runtime.js": `const ua = () => {};\nexport { ua as render };\n`,
    }),
    makeChunk("static/index-AppEntry", {
      "runtime.js": `import { render as r } from "../katex-BZy9Y_85/runtime.js";\nr();\n`,
    }),
  ]);
  const before = chunkCode(artifact, "static/index-AppEntry");
  const { manifest } = renameVendorExports({
    artifact,
    operations: [vendorOp()],
  });
  assert.equal(manifest.counts.rewrites, 0);
  assert.equal(chunkCode(artifact, "static/index-AppEntry"), before);
});

test("dynamic import is not rewritten", () => {
  const artifact = makeArtifact([
    makeChunk("static/katex-BZy9Y_85", {
      "runtime.js": `const ua = () => {};\nexport { ua as render };\n`,
    }),
    makeChunk("static/index-AppEntry", {
      "runtime.js": `async function load() { return await import("../katex-BZy9Y_85/runtime.js"); }\n`,
    }),
  ]);
  const before = chunkCode(artifact, "static/index-AppEntry");
  const { manifest } = renameVendorExports({
    artifact,
    operations: [vendorOp()],
  });
  assert.equal(manifest.counts.rewrites, 0);
  assert.equal(chunkCode(artifact, "static/index-AppEntry"), before);
});

test("default and namespace imports are not rewritten", () => {
  const artifact = makeArtifact([
    makeChunk("static/katex-BZy9Y_85", {
      "runtime.js": `const ua = () => {};\nexport { ua as render };\n`,
    }),
    makeChunk("static/cons-def", {
      "runtime.js": `import Foo from "../katex-BZy9Y_85/runtime.js";\nFoo();\n`,
    }),
    makeChunk("static/cons-ns", {
      "runtime.js": `import * as Foo from "../katex-BZy9Y_85/runtime.js";\nFoo.ua();\n`,
    }),
  ]);
  const beforeDef = chunkCode(artifact, "static/cons-def");
  const beforeNs = chunkCode(artifact, "static/cons-ns");
  const { manifest } = renameVendorExports({
    artifact,
    operations: [vendorOp()],
  });
  assert.equal(manifest.counts.rewrites, 0);
  assert.equal(chunkCode(artifact, "static/cons-def"), beforeDef);
  assert.equal(chunkCode(artifact, "static/cons-ns"), beforeNs);
});

test("missing vendor chunk fails closed naming op id and chunkId", () => {
  const artifact = makeArtifact([
    makeChunk("static/index-AppEntry", { "runtime.js": "export {};\n" }),
  ]);
  assert.throws(
    () => renameVendorExports({ artifact, operations: [vendorOp()] }),
    (err) =>
      /op_katex/.test(err.message) &&
      /static\/katex-BZy9Y_85/.test(err.message) &&
      /missing chunk/.test(err.message)
  );
});

test('level "suppress" ops are ignored', () => {
  const artifact = makeArtifact([
    makeChunk("static/pdf.worker-U2G3g_2t", {
      "runtime.js": `const ua = () => {};\nexport { ua as render };\n`,
    }),
    makeChunk("static/index-AppEntry", {
      "runtime.js": `import { ua as r } from "../pdf.worker-U2G3g_2t/runtime.js";\nr();\n`,
    }),
  ]);
  const before = chunkCode(artifact, "static/index-AppEntry");
  const { manifest } = renameVendorExports({
    artifact,
    operations: [
      vendorOp({
        id: "op_suppress",
        level: "suppress",
        chunkPath: "static/pdf.worker-U2G3g_2t.js",
      }),
    ],
  });
  assert.equal(manifest.counts.considered, 0);
  assert.equal(manifest.counts.rewrites, 0);
  assert.equal(chunkCode(artifact, "static/index-AppEntry"), before);
});

test("multiple vendor ops across multiple consumers all rewritten", () => {
  const artifact = makeArtifact([
    makeChunk("static/katex-BZy9Y_85", {
      "runtime.js": `const ua = 1;\nconst m4 = "0.16";\nexport { ua as render, m4 as version };\n`,
    }),
    makeChunk("static/other-vendor", {
      "runtime.js": `const zz = 2;\nexport { zz as parse };\n`,
    }),
    makeChunk("static/consumer-a", {
      "runtime.js": `import { ua as r } from "../katex-BZy9Y_85/runtime.js";\nimport { zz as p } from "../other-vendor/runtime.js";\nr(); p();\n`,
    }),
    makeChunk("static/consumer-b", {
      "runtime.js": `import { m4 as v } from "../katex-BZy9Y_85/runtime.js";\nv;\n`,
    }),
  ]);
  const { manifest } = renameVendorExports({
    artifact,
    operations: [
      vendorOp({ id: "op_katex", chunkPath: "static/katex-BZy9Y_85.js", level: "swap", package: "katex", version: "0.16.11", subpath: "dist/katex.mjs" }),
      vendorOp({ id: "op_other", chunkPath: "static/other-vendor.js", level: "boundary-rename" }),
    ],
  });
  assert.equal(manifest.counts.rewrites, 3);
  const a = chunkCode(artifact, "static/consumer-a");
  assert.match(a, /import\s*\{\s*render as r\s*\}\s*from\s*"\.\.\/katex-BZy9Y_85\/runtime\.js"/);
  assert.match(a, /import\s*\{\s*parse as p\s*\}\s*from\s*"\.\.\/other-vendor\/runtime\.js"/);
  const b = chunkCode(artifact, "static/consumer-b");
  assert.match(b, /import\s*\{\s*version as v\s*\}\s*from\s*"\.\.\/katex-BZy9Y_85\/runtime\.js"/);
});

test("same local name across chunks stays isolated", () => {
  const artifact = makeArtifact([
    makeChunk("static/katex-BZy9Y_85", {
      "runtime.js": `const ua = () => {};\nexport { ua as render };\n`,
    }),
    makeChunk("static/other-chunk", {
      "runtime.js": `const ua = 42;\nexport function doThing() { return ua; }\n`,
    }),
    makeChunk("static/consumer", {
      "runtime.js": `import { doThing } from "../other-chunk/runtime.js";\nimport { ua as r } from "../katex-BZy9Y_85/runtime.js";\ndoThing(); r();\n`,
    }),
  ]);
  const { manifest } = renameVendorExports({
    artifact,
    operations: [vendorOp()],
  });
  assert.equal(manifest.counts.rewrites, 1);
  const otherCode = chunkCode(artifact, "static/other-chunk");
  assert.match(otherCode, /const ua = 42/);
  const consumerCode = chunkCode(artifact, "static/consumer");
  assert.match(consumerCode, /import\s*\{\s*doThing\s*\}\s*from\s*"\.\.\/other-chunk\/runtime\.js"/);
  assert.match(consumerCode, /import\s*\{\s*render as r\s*\}\s*from\s*"\.\.\/katex-BZy9Y_85\/runtime\.js"/);
});

test("non-runtime chunk entry files are rewritten using the imported entry file", () => {
  const artifact = makeArtifact([
    makeChunk("static/katex-BZy9Y_85", {
      "entry.js": `const ua = () => {};\nexport { ua as render };\n`,
    }),
    makeChunk("static/index-AppEntry", {
      "entry.js": `import { ua as r } from "../katex-BZy9Y_85/entry.js";\nr();\n`,
    }),
  ]);

  const { manifest } = renameVendorExports({
    artifact,
    operations: [vendorOp()],
  });

  assert.equal(manifest.counts.rewrites, 1);
  assert.match(chunkCode(artifact, "static/index-AppEntry"), /import\s*\{\s*render as r\s*\}\s*from\s*"\.\.\/katex-BZy9Y_85\/entry\.js"/);
});
