import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { buildJsGolden, buildRustGolden, listFiles } from "./pipeline_impl_golden_lib.mjs";

test("rust planner snapshot matches js-derived snapshot on mock fixture", async () => {
  const tmp = mkdtempSync(join(tmpdir(), "debundle-planner-parity-"));
  const jsOut = join(tmp, "js");
  const rustOut = join(tmp, "rust");
  await buildJsGolden(jsOut);
  buildRustGolden(rustOut, resolveRustBin());

  const jsSnapshot = buildJsSnapshot(jsOut);
  const rustSnapshot = JSON.parse(readFileSync(join(rustOut, "planner_snapshot.json"), "utf8"));
  assert.deepEqual(normalize(jsSnapshot), normalize(rustSnapshot));
});

function resolveRustBin() {
  const runfiles = process.env.TEST_SRCDIR;
  const workspace = process.env.TEST_WORKSPACE;
  const rel = process.env.RUST_DEBUNDLE_BIN;
  assert.ok(runfiles && workspace && rel, "bazel runfiles env for rust bin missing");
  return join(runfiles, workspace, rel);
}

function buildJsSnapshot(jsRoot) {
  const chunkManifest = JSON.parse(readFileSync(join(jsRoot, "chunks.manifest.json"), "utf8"));
  const modules = chunkManifest.chunks.map((c) => {
    const code = readFileSync(join(jsRoot, c.chunkId, "entry.js"), "utf8");
    const imports = [...code.matchAll(/import\s+["']\.\/([^"']+)["']/g)].map((m) =>
      m[1].replace(/\/entry\.js$/, ".js")
    );
    return {
      id: c.sourcePath,
      imports: imports.sort(),
      hasTopLevelEffects: code.includes("new ") || code.includes("window.") || code.includes("document."),
    };
  });
  const selectedModules = modules.map((m) => m.id).sort();
  const extractionGroups = deriveExtractionGroupsFromChunkManifest(chunkManifest);
  return {
    schemaVersion: 1,
    modules,
    selectedModules,
    extractionGroups,
    rationale: "owner-graph connected components with side-effect order constraints",
  };
}

function deriveExtractionGroupsFromChunkManifest(chunkManifest) {
  const groups = chunkManifest.chunks.map((chunk) => {
    if (Array.isArray(chunk.sourcePaths) && chunk.sourcePaths.length > 0) {
      return [...chunk.sourcePaths].sort();
    }
    if (Array.isArray(chunk.members) && chunk.members.length > 0) {
      return [...chunk.members].sort();
    }
    if (typeof chunk.sourcePath === "string") {
      return [chunk.sourcePath];
    }
    throw new Error(`unable to derive source-module group for chunk ${JSON.stringify(chunk)}`);
  });
  return groups.sort((a, b) => a[0].localeCompare(b[0]));
}

function normalize(snapshot) {
  const modules = [...snapshot.modules]
    .map((m) => ({
      id: m.id,
      imports: [...m.imports].sort(),
      hasTopLevelEffects: Boolean(m.hasTopLevelEffects),
    }))
    .sort((a, b) => a.id.localeCompare(b.id));
  const selectedModules = [...snapshot.selectedModules].sort();
  const extractionGroups = snapshot.extractionGroups
    .map((g) => [...g].sort())
    .sort((a, b) => a[0].localeCompare(b[0]));
  return {
    schemaVersion: 1,
    modules,
    selectedModules,
    extractionGroups,
    rationale: snapshot.rationale,
  };
}
