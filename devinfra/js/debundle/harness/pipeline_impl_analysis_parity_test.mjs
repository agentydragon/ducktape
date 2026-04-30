import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { buildRustGolden } from "./pipeline_impl_golden_lib.mjs";
import { analyzeRuntimeBoundaryCode } from "../analysis/boundary.mjs";

const FIXTURE_ROOT = fileURLToPath(new URL("./testdata/mock_browser_bundle/", import.meta.url));

test("rust analysis snapshot strictly matches js boundary semantics on mock fixture", async () => {
  const tmp = mkdtempSync(join(tmpdir(), "debundle-analysis-parity-"));
  const rustOut = join(tmp, "rust");
  buildRustGolden(rustOut, resolveRustBin());

  const jsSnapshot = buildJsAnalysisContractFromMockFixture();
  const rustSnapshot = JSON.parse(readFileSync(join(rustOut, "analysis_snapshot.json"), "utf8"));
  assert.deepEqual(normalize(jsSnapshot), normalize(rustSnapshot));
});

function resolveRustBin() {
  const runfiles = process.env.TEST_SRCDIR;
  const workspace = process.env.TEST_WORKSPACE;
  const rel = process.env.RUST_DEBUNDLE_BIN;
  assert.ok(runfiles && workspace && rel, "bazel runfiles env for rust bin missing");
  return join(runfiles, workspace, rel);
}

function buildJsAnalysisContractFromMockFixture() {
  const generatedRoot = join(FIXTURE_ROOT, "generated");
  const jsList = readFileSync(join(generatedRoot, "extracted/js-files.txt"), "utf8")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  const modules = jsList
    .map((sourcePath, idx) => {
      const code = readFileSync(join(generatedRoot, "snapshot", sourcePath), "utf8");
      const analysis = analyzeRuntimeBoundaryCode(code, { chunkId: sourcePath.replace(/\.js$/, "") });
      const imports = (analysis.imports ?? [])
        .map((record) => String(record.source ?? ""))
        .filter((src) => src.startsWith("./") || src.startsWith("../"))
        .map((src) => resolveRelativeSource(sourcePath, src))
        .sort();
      const owners = (analysis.owners ?? []).map((o) => o.id).sort();
      const programItems = (analysis.programItems ?? []).map((i) => i.id).sort();
      const sideEffects = (analysis.sideEffects ?? []).map((s) => s.id).sort();
      return {
        moduleId: sourcePath,
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
    })
    .sort((a, b) => a.moduleId.localeCompare(b.moduleId));

  return { schemaVersion: 1, contract: "analysis_ir_parity_v1", modules };
}

function resolveRelativeSource(sourcePath, spec) {
  const base = sourcePath.split("/").slice(0, -1);
  for (const part of spec.split("/")) {
    if (part === "." || part === "") continue;
    if (part === "..") {
      base.pop();
    } else {
      base.push(part);
    }
  }
  let out = base.join("/");
  if (!out.endsWith(".js")) out += ".js";
  return out;
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
