import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, mkdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

import { analyzeRuntimeBoundaryCode } from "../analysis/boundary.mjs";

test("rust analysis snapshot matches js boundary semantics on synthetic non-mock fixture", () => {
  const tmp = mkdtempSync(join(tmpdir(), "debundle-analysis-fixture-"));
  const inputRoot = join(tmp, "snapshot");
  const outRoot = join(tmp, "out");
  mkdirSync(join(inputRoot, "static"), { recursive: true });

  const aPath = "static/alpha.js";
  const bPath = "static/beta.js";
  const aCode = `import "./beta.js";\nexport const foo = 1;\nwindow.__x = foo;\n`;
  const bCode = `const answer = 42;\nexport function ping(){ return answer; }\nconsole.log(ping());\n`;
  writeFileSync(join(inputRoot, aPath), aCode);
  writeFileSync(join(inputRoot, bPath), bCode);
  writeFileSync(join(tmp, "js-files.txt"), `${aPath}\n${bPath}\n`);

  const run = spawnSync(resolveRustBin(), ["--input-root", inputRoot, "--js-list", join(tmp, "js-files.txt"), "--out-root", outRoot], {
    encoding: "utf8",
  });
  assert.equal(run.status, 0, run.stderr);

  const rust = JSON.parse(readFileSync(join(outRoot, "analysis_snapshot.json"), "utf8"));
  const js = {
    schemaVersion: 1,
    contract: "analysis_ir_parity_v1",
    modules: [
      buildModule(aPath, aCode, 0),
      buildModule(bPath, bCode, 1),
    ].sort((x, y) => x.moduleId.localeCompare(y.moduleId)),
  };
  assert.deepEqual(normalize(js), normalize(rust));
});

function buildModule(moduleId, code, idx) {
  const analysis = analyzeRuntimeBoundaryCode(code, { chunkId: moduleId.replace(/\.js$/, "") });
  const imports = (analysis.imports ?? [])
    .map((record) => String(record.source ?? ""))
    .filter((src) => src.startsWith("./"))
    .map((src) => `static/${src.slice(2)}`)
    .sort();
  const owners = (analysis.owners ?? []).map((o) => o.id).sort();
  const programItems = (analysis.programItems ?? []).map((i) => i.id).sort();
  const sideEffects = (analysis.sideEffects ?? []).map((s) => s.id).sort();
  return {
    moduleId,
    ownerId: `owner_${String(idx).padStart(4, "0")}`,
    programItemId: `item_${String(idx).padStart(4, "0")}`,
    imports,
    hasTopLevelEffects: code.includes("new ") || code.includes("window.") || code.includes("document."),
    exportCount: (code.match(/\bexport\b/g) ?? []).length,
    ownerIds: owners,
    programItemIds: programItems,
    sideEffectIds: sideEffects,
    ownerCount: owners.length,
    programItemCount: programItems.length,
    sideEffectCount: sideEffects.length,
  };
}

function normalize(snapshot) {
  return {
    schemaVersion: 1,
    contract: snapshot.contract,
    modules: [...snapshot.modules]
      .map((m) => ({
        moduleId: m.moduleId,
        ownerId: m.ownerId,
        programItemId: m.programItemId,
        imports: [...m.imports].sort(),
        hasTopLevelEffects: Boolean(m.hasTopLevelEffects),
        exportCount: Number(m.exportCount),
        ownerIds: [...(m.ownerIds ?? [])].sort(),
        programItemIds: [...(m.programItemIds ?? [])].sort(),
        sideEffectIds: [...(m.sideEffectIds ?? [])].sort(),
        ownerCount: Number(m.ownerCount ?? (m.ownerIds ?? []).length),
        programItemCount: Number(m.programItemCount ?? (m.programItemIds ?? []).length),
        sideEffectCount: Number(m.sideEffectCount ?? (m.sideEffectIds ?? []).length),
      }))
      .sort((a, b) => a.moduleId.localeCompare(b.moduleId)),
  };
}

function resolveRustBin() {
  const runfiles = process.env.TEST_SRCDIR;
  const workspace = process.env.TEST_WORKSPACE;
  const rel = process.env.RUST_DEBUNDLE_BIN;
  assert.ok(runfiles && workspace && rel, "bazel runfiles env for rust bin missing");
  return join(runfiles, workspace, rel);
}



test("rust analysis snapshot matches js boundary semantics on re-export/side-effect fixture", () => {
  const tmp = mkdtempSync(join(tmpdir(), "debundle-analysis-fixture-2-"));
  const inputRoot = join(tmp, "snapshot");
  const outRoot = join(tmp, "out");
  mkdirSync(join(inputRoot, "static"), { recursive: true });

  const aPath = "static/root.js";
  const bPath = "static/dep.js";
  const aCode = `export { value } from "./dep.js";\nclass Box {}\nnew Box();\n`;
  const bCode = `export const value = 7;\ndocument.title = String(value);\n`;
  writeFileSync(join(inputRoot, aPath), aCode);
  writeFileSync(join(inputRoot, bPath), bCode);
  writeFileSync(join(tmp, "js-files.txt"), `${aPath}\n${bPath}\n`);

  const run = spawnSync(resolveRustBin(), ["--input-root", inputRoot, "--js-list", join(tmp, "js-files.txt"), "--out-root", outRoot], {
    encoding: "utf8",
  });
  assert.equal(run.status, 0, run.stderr);

  const rust = JSON.parse(readFileSync(join(outRoot, "analysis_snapshot.json"), "utf8"));
  const js = {
    schemaVersion: 1,
    contract: "analysis_ir_parity_v1",
    modules: [
      buildModule(aPath, aCode, 0),
      buildModule(bPath, bCode, 1),
    ].sort((x, y) => x.moduleId.localeCompare(y.moduleId)),
  };
  assert.deepEqual(normalize(js), normalize(rust));
});
