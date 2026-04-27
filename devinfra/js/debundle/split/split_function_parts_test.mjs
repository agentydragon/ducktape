import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import {
  createWebFixtureRoots,
  listJsFiles,
  parseModuleCode,
  readUtf8,
  runNodeScript,
  writeSnapshotFixture,
} from "../test_support/fixture_lib.mjs";
import { computeJsAsts } from "../common/compute_js_asts_lib.mjs";
import { loadJsChunks } from "../common/load_js_chunks_lib.mjs";
import { rewriteChunkEntrySpecifiers } from "../transforms/rewrite_chunk_entry_specifiers_lib.mjs";
import { writeJsTree } from "../transforms/write_js_tree_lib.mjs";
import { normalizeJsChunks, splitFunctionParts } from "./split_function_parts_lib.mjs";

const ENTRY_FILE = "entry.js";

function writeSplitSnapshotFixture(prefix) {
  const { extractedRoot, outRoot: outDir, snapshotRoot } = createWebFixtureRoots(prefix);
  writeSnapshotFixture({
    extractedRoot,
    files: {
      "static/a.js": `import { b as c } from "./b.js";function loadB(){return import("./b.js").then(({b})=>b)}function spawnWorker(){return new Worker("/static/b.js",{name:"b"})}console.log("static="+c);const a=c+1;console.log("a="+a);await loadB().then((b)=>console.log("dynamic="+b));export{a,loadB,spawnWorker};\n`,
      "static/b.js": `console.log("b-side-effect");const b=2;export{b};\n`,
    },
    jsFiles: ["static/a.js", "static/b.js"],
    snapshotRoot,
  });
  return { extractedRoot, outDir, snapshotRoot };
}

test("normalizeJsChunks preserves original cross-chunk imports and worker URLs", async () => {
  const { extractedRoot, outDir, snapshotRoot } = writeSplitSnapshotFixture("debundle-split-snapshot-test-");

  const loaded = loadJsChunks({
    jsListPath: join(extractedRoot, "js-files.txt"),
    inputRoot: snapshotRoot,
  });
  const parsed = computeJsAsts({
    artifact: loaded.artifact,
  });
  const { artifact, manifest } = await normalizeJsChunks({
    artifact: parsed.artifact,
    jobs: 2,
  });
  writeJsTree({
    artifact,
    force: true,
    outDir,
  });

  assert.equal(manifest.counts.chunks, 2);
  assert.equal(manifest.chunks[0].chunkId, "static/a");
  assert.equal(manifest.chunks[1].chunkId, "static/b");
  assert.equal(manifest.counts.unresolvedExports, 0);

  const aRuntime = readUtf8(join(outDir, "static", "a", ENTRY_FILE));
  const aChunkManifest = JSON.parse(readUtf8(join(outDir, "static", "a", "manifest.json")));
  assert.equal(aChunkManifest.entryFile, ENTRY_FILE);
  assert.match(aRuntime, /from "\.\/b\.js"/);
  assert.match(aRuntime, /import\("\.\/b\.js"\)/);
  assert.match(aRuntime, /new Worker\("\/static\/b\.js", \{/);
  assert.doesNotMatch(aRuntime, /new URL\(/);
  assert.equal(
    listJsFiles(join(outDir, "static", "a")).some((file) => file.startsWith("module-")),
    false
  );
  assert.equal(existsSync(join(outDir, "static", "a", "prelude.js")), false);
  assert.equal(existsSync(join(outDir, "static", "a", "exports.js")), false);
  assert.equal(existsSync(join(outDir, "static", "a", "parts")), false);
});

test("rewriteChunkEntrySpecifiers writes runnable parseable JS tree output", async () => {
  const { extractedRoot, outDir, snapshotRoot } = writeSplitSnapshotFixture("debundle-split-snapshot-runnable-");

  const loaded = loadJsChunks({
    jsListPath: join(extractedRoot, "js-files.txt"),
    inputRoot: snapshotRoot,
  });
  const parsed = computeJsAsts({
    artifact: loaded.artifact,
  });
  const normalized = await normalizeJsChunks({
    artifact: parsed.artifact,
    jobs: 2,
  });
  const { artifact } = rewriteChunkEntrySpecifiers({
    artifact: normalized.artifact,
  });
  writeJsTree({
    artifact,
    force: true,
    outDir,
  });

  assert.deepEqual(JSON.parse(readUtf8(join(outDir, "package.json"))), { type: "module" });
  assert.deepEqual(runNodeScript(join(outDir, "static", "a", ENTRY_FILE)), runNodeScript(join(snapshotRoot, "static", "a.js")));

  for (const chunk of ["a", "b"]) {
    const chunkDir = join(outDir, "static", chunk);
    for (const file of listJsFiles(chunkDir)) {
      parseModuleCode(readUtf8(join(chunkDir, file)));
    }
  }

  const pipelineManifest = JSON.parse(readUtf8(join(outDir, "manifest.json")));
  assert.equal("uiVersion" in pipelineManifest, false);
  assert.equal(pipelineManifest.counts.chunks, 2);
  assert.equal(pipelineManifest.counts.parts, 0);
});

test("normalizeJsChunks uses a fixed canonical entry path", async () => {
  const { extractedRoot, snapshotRoot } = writeSplitSnapshotFixture("debundle-split-snapshot-canonical-entry-");

  const loaded = loadJsChunks({
    jsListPath: join(extractedRoot, "js-files.txt"),
    inputRoot: snapshotRoot,
  });
  const parsed = computeJsAsts({
    artifact: loaded.artifact,
  });
  const normalized = await normalizeJsChunks({
    artifact: parsed.artifact,
    jobs: 2,
  });

  assert.equal(normalized.artifact.chunks.get("static/a")?.entryFile, ENTRY_FILE);
  assert.equal(normalized.artifact.chunks.get("static/b")?.entryFile, ENTRY_FILE);

  await assert.rejects(
    normalizeJsChunks({
      artifact: parsed.artifact,
      entryFile: "shell/entry.js",
      jobs: 2,
    }),
    /no longer accepts entryFile/
  );
});

test("splitFunctionParts emits parts when a chunk has safely splittable owners", async () => {
  const { extractedRoot, snapshotRoot } = writeSplitSnapshotFixture("debundle-split-snapshot-parts-");

  const loaded = loadJsChunks({
    jsListPath: join(extractedRoot, "js-files.txt"),
    inputRoot: snapshotRoot,
  });
  const parsed = computeJsAsts({
    artifact: loaded.artifact,
  });
  const normalized = await normalizeJsChunks({
    artifact: parsed.artifact,
    jobs: 2,
  });
  const { manifest } = await splitFunctionParts({
    artifact: normalized.artifact,
    jobs: 2,
  });

  const aChunk = manifest.chunks.find((chunk) => chunk.chunkId === "static/a");
  assert.ok(aChunk);
  assert.ok(manifest.counts.parts > 0);
});

test("splitFunctionParts requires normalizeJsChunks first", async () => {
  const { extractedRoot, snapshotRoot } = writeSplitSnapshotFixture("debundle-split-snapshot-needs-normalize-");

  const loaded = loadJsChunks({
    jsListPath: join(extractedRoot, "js-files.txt"),
    inputRoot: snapshotRoot,
  });
  const parsed = computeJsAsts({
    artifact: loaded.artifact,
  });

  await assert.rejects(
    splitFunctionParts({
      artifact: parsed.artifact,
      jobs: 2,
    }),
    /requires normalizeJsChunks first/
  );
});
