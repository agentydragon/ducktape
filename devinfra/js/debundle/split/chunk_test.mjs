import assert from "node:assert/strict";
import { join } from "node:path";
import test from "node:test";

import {
  createTempFixtureRoot,
  listJsFiles,
  parseModuleCode,
  readUtf8,
  runNodeScript,
  writeRunnableFixture,
} from "../test_support/fixtures.mjs";
import { splitScopeHoistedChunk, writeSplitOutput } from "./chunk.mjs";

const ENTRY_FILE = "entry.js";

const FIXTURE_CHUNK = `import { x as ext } from "./dep.js";
const a = 1, b = a + ext;
function c() {
  return b;
}
function d() {
  return c();
}
function e() {
  return f();
}
function f() {
  return e();
}
console.log(d());
export { d as run, e as cycle };
`;

test("splitScopeHoistedChunk emits runtime metadata and groups cyclic functions together", () => {
  const result = splitScopeHoistedChunk(FIXTURE_CHUNK, {
    chunkId: "fixture",
    sourcePath: "fixture.js",
  });

  assert.equal(result.manifest.entryFile, ENTRY_FILE);
  assert.equal(result.manifest.counts.importDeclarations, 1);
  assert.equal(result.manifest.counts.exportAliases, 2);
  assert.equal(result.manifest.counts.topLevelSideEffects, 1);
  assert.equal(result.manifest.unresolvedExports.length, 0);
  assert.equal(result.files.has("prelude.js"), false);
  assert.equal(result.files.has("exports.js"), false);
  assert.equal(
    [...result.files.keys()].some((file) => file.startsWith("module-")),
    false
  );
  assert.ok(result.manifest.counts.parts >= 1);

  const cyclePart = result.manifest.parts.find((part) => part.exportedNames.includes("e"));
  assert.ok(cyclePart);
  assert.deepEqual(cyclePart.exportedNames, ["e", "f"]);

  const runtimeFile = result.files.get(ENTRY_FILE);
  assert.match(runtimeFile, /Executable chunk entry/);
  assert.match(runtimeFile, /import \{ e, f \} from "\.\/parts\/part-\d+\.js";/);
  assert.match(runtimeFile, /console\.log\(d\(\)\);/);
});

test("splitScopeHoistedChunk rejects invalid entry file paths", () => {
  assert.throws(
    () =>
      splitScopeHoistedChunk(`export const value = 1;\n`, {
        chunkId: "invalid-entry",
        entryFile: "../entry.js",
        sourcePath: "invalid-entry.js",
      }),
    /Invalid chunk entry file/
  );
});

test("splitScopeHoistedChunk rewrites static runtime imports", () => {
  const rewritten = splitScopeHoistedChunk(`import { x as ext } from "./dep.js"; export { ext as renamed };`, {
    chunkId: "rewritten",
    rewriteEntryImportSource: (source) => (source === "./dep.js" ? `../dep/${ENTRY_FILE}` : source),
    sourcePath: "rewritten.js",
  });

  assert.match(rewritten.files.get(ENTRY_FILE), /from "\.\.\/dep\/entry\.js"/);
  assert.equal(rewritten.manifest.counts.unresolvedExports, 0);
});

test("splitScopeHoistedChunk rewrites Worker URLs into runtime-relative module URLs", () => {
  const workerRewrite = splitScopeHoistedChunk(
    `function spawn(){return new Worker("/static/worker.js",{name:"w"})}export{spawn};`,
    {
      chunkId: "worker-rewrite",
      rewriteEntryImportSource: (source) => (source === "/static/worker.js" ? `../worker/${ENTRY_FILE}` : source),
      sourcePath: "worker-rewrite.js",
    }
  );

  const workerPart = [...workerRewrite.files.entries()].find(
    ([file, text]) => file.startsWith("parts/") && text.includes("function spawn")
  );
  assert.ok(workerPart);
  assert.match(workerPart[1], /new Worker\(new URL\("\.\.\/\.\.\/worker\/entry\.js", import\.meta\.url\),/);
});

test("writeSplitOutput emits parseable files and preserves runnable semantics", () => {
  const result = splitScopeHoistedChunk(FIXTURE_CHUNK, {
    chunkId: "fixture",
    sourcePath: "fixture.js",
  });
  const outDir = createTempFixtureRoot("debundle-split-test-");
  writeSplitOutput(result, outDir);

  for (const file of listJsFiles(outDir)) {
    parseModuleCode(readUtf8(join(outDir, file)));
  }

  const runnableFixture = `function b(c){return c+2}const d=b(5);console.log("value:"+d);export{b as run};`;
  const runnableDir = createTempFixtureRoot("debundle-split-runnable-test-");
  writeRunnableFixture(runnableDir, {
    files: {
      "original.js": `${runnableFixture}\n`,
    },
  });

  const runnableResult = splitScopeHoistedChunk(runnableFixture, {
    chunkId: "runnable",
    sourcePath: "original.js",
  });
  writeSplitOutput(runnableResult, join(runnableDir, "split"));

  const originalRun = runNodeScript(join(runnableDir, "original.js"));
  const runtimeRun = runNodeScript(join(runnableDir, "split", ENTRY_FILE));
  assert.deepEqual(runtimeRun, originalRun);
  assert.ok(listJsFiles(join(runnableDir, "split", "parts")).length > 0);
});

test("splitScopeHoistedChunk keeps hoisting hazards in runtime", () => {
  const hoistingHazard = `var Xi;Xi={DEBUG:1};function read(){return Xi.DEBUG}console.log(read());export{read};`;
  const hazardDir = createTempFixtureRoot("debundle-split-hoisting-test-");
  writeRunnableFixture(hazardDir, {
    files: {
      "original.js": `${hoistingHazard}\n`,
    },
  });

  const hazardResult = splitScopeHoistedChunk(hoistingHazard, {
    chunkId: "hazard",
    sourcePath: "original.js",
  });
  writeSplitOutput(hazardResult, join(hazardDir, "split"));

  assert.equal(hazardResult.manifest.counts.parts, 0);
  assert.match(
    hazardResult.manifest.keptTopLevelDeclarations[1].unsafeReason,
    /depends_on_unsplit_top_level_binding:Xi/
  );
  assert.deepEqual(runNodeScript(join(hazardDir, "split", ENTRY_FILE)), runNodeScript(join(hazardDir, "original.js")));
});

test("splitScopeHoistedChunk keeps top-level write hazards in runtime", () => {
  const topLevelWriteHazard = `var target;function setTarget(value){target=value}setTarget(1);console.log(target);export{setTarget};`;
  const writeHazardDir = createTempFixtureRoot("debundle-split-write-hazard-test-");
  writeRunnableFixture(writeHazardDir, {
    files: {
      "original.js": `${topLevelWriteHazard}\n`,
    },
  });

  const writeHazardResult = splitScopeHoistedChunk(topLevelWriteHazard, {
    chunkId: "write-hazard",
    sourcePath: "original.js",
  });
  writeSplitOutput(writeHazardResult, join(writeHazardDir, "split"));

  assert.equal(writeHazardResult.manifest.counts.parts, 0);
  assert.match(
    writeHazardResult.manifest.keptTopLevelDeclarations[1].unsafeReason,
    /depends_on_unsplit_top_level_binding:target/
  );
  assert.deepEqual(
    runNodeScript(join(writeHazardDir, "split", ENTRY_FILE)),
    runNodeScript(join(writeHazardDir, "original.js"))
  );
});
