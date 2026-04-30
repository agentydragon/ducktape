import { readFileSync } from "node:fs";
import { join, posix } from "node:path";
import * as t from "@babel/types";
import { analyzeRuntimeBoundaryAst } from "../analysis/boundary.mjs";
import { DEFAULT_PARSER_OPTIONS, writeJsonFile } from "../common/parser_options.mjs";
import {
  createChunk,
  createFile,
  deleteArtifactChunkManifest,
  getArtifactChunkManifest,
  getArtifactManifestOrDerived,
  getChunk,
  getChunkEntryFile,
  getChunkEntryPath,
  getChunkFile,
  removeFiles,
  requirePipelineArtifact,
  setArtifactChunkManifest,
  setArtifactManifest,
  setChunk,
} from "../common/artifact.mjs";
import {
  formatDurationSince,
  logProgress,
  prepareOutputDir,
  relativeWorkspacePath,
  resolveWorkspacePath,
} from "../common/io.mjs";
import { serializeGeneratedJsFile } from "../split/chunk.mjs";
import { buildSelectedModuleLoweringMetadata, extractSelectedModulePlanInAst } from "./init_region.mjs";
import { deriveSelectedModuleTarget, planSelectedAtomicModules } from "./planner.mjs";

export function extractAtomicModules({
  artifact,
  boundaryAnalysisDir = undefined,
  chunkIds,
  file = undefined,
  pruneOtherChunks = true,
  force = false,
  reportOutDir = undefined,
  reportSummaryPath = undefined,
  selectedOwnerIdsByChunk = undefined,
  targetDir = "modules",
}) {
  requirePipelineArtifact(artifact, "extractAtomicModules");
  let artifactManifest = getArtifactManifestOrDerived(artifact);

  const selectedChunkIds = normalizeChunkIds(chunkIds);
  const startedAt = process.hrtime.bigint();
  const resolvedBoundaryAnalysisDir = boundaryAnalysisDir ? resolveWorkspacePath(boundaryAnalysisDir) : null;
  const reports = [];
  const applied = [];

  let resolvedReportOutDir = null;
  let resolvedReportSummaryPath = null;
  if (reportOutDir) {
    resolvedReportOutDir = resolveWorkspacePath(reportOutDir);
    prepareOutputDir(resolvedReportOutDir, { force });
    resolvedReportSummaryPath = resolveWorkspacePath(reportSummaryPath ?? posix.join(reportOutDir, "summary.json"));
  }

  if (pruneOtherChunks) {
    pruneArtifactToChunkIds(artifact, selectedChunkIds);
    artifactManifest = getArtifactManifestOrDerived(artifact);
  }

  logProgress(`atomic-modules start chunks=${selectedChunkIds.length}`);

  for (const chunkId of selectedChunkIds) {
    const targetFile = file ? normalizeRelativeFile(file) : getChunkEntryPath(artifact, chunkId);
    if (!targetFile) {
      throw new Error(`extractAtomicModules could not determine entry file for chunk: ${chunkId}`);
    }
    const runtimeFile = file ? getChunkFile(artifact, chunkId, targetFile) : getChunkEntryFile(artifact, chunkId);
    if (!runtimeFile?.ast) {
      throw new Error(`extractAtomicModules missing entry AST for chunk: ${chunkId}`);
    }

    const runtimeHeaderLines = runtimeFile.headerLines ?? [];
    const runtimeParserOptions = runtimeFile.parserOptions ?? DEFAULT_PARSER_OPTIONS;
    const chunkStartedAt = process.hrtime.bigint();
    const analysisStartedAt = process.hrtime.bigint();
    const analysis = resolvedBoundaryAnalysisDir
      ? readBoundaryAnalysis(join(resolvedBoundaryAnalysisDir, `${chunkId}.json`))
      : analyzeRuntimeBoundaryAst(runtimeFile.ast, {
          chunkId,
          manifestPath: `${chunkId}/manifest.json`,
          runtimePath: `${chunkId}/${targetFile}`,
          uiVersion: artifactManifest?.uiVersion ?? null,
        });
    const analysisMs = durationMsSince(analysisStartedAt);
    const planningStartedAt = process.hrtime.bigint();
    const plan = planSelectedAtomicModules(
      {
        analysis,
        // No source-code input: we cloneNode the AST rather than serialize+
        // re-parse, so byte counts in atomic-unit metrics are not computed
        // (manifest reports null for those bytes — a fair tradeoff for a
        // ~1s saving per chunk).
        code: null,
        programBody: runtimeFile.ast.program.body,
      },
      {
        selectedOwnerIds: selectedOwnerIdsByChunk?.[chunkId] ?? null,
      }
    );
    const planningMs = durationMsSince(planningStartedAt);
    // Lowering destructively mutates the AST; clone the runtime AST so the
    // original chunk file is preserved and later module-regrouping or
    // logical-materialization work can start from a clean tree.
    const parseStartedAt = process.hrtime.bigint();
    const loweringAst = t.cloneNode(runtimeFile.ast, true);
    const parseMs = durationMsSince(parseStartedAt);
    const loweringStartedAt = process.hrtime.bigint();
    const result = extractSelectedModulePlanInAst(loweringAst, plan, {
      analysis,
      chunkId,
      file: targetFile,
      headerLines: runtimeHeaderLines,
      idPrefix: "atomic_module",
      targetDir,
    });
    const loweringMs = durationMsSince(loweringStartedAt);

    const currentModules = plan.modulePlans.map((modulePlan, index) => {
      const target = deriveSelectedModuleTarget(modulePlan, index, { targetDir });
      return {
        ...cloneModulePlan(modulePlan),
        initName: target.init,
        targetFile: target.file,
      };
    });
    const moduleByTargetFile = new Map(currentModules.map((modulePlan) => [modulePlan.targetFile, modulePlan]));
    const chunk = getChunk(artifact, chunkId);
    const writebackStartedAt = process.hrtime.bigint();
    const nextChunk = createChunk({
      chunkId,
      entryFile: targetFile,
      files: [...result.jsFiles.entries()].map(([relativePath, fileArtifact]) => {
        const modulePlan = moduleByTargetFile.get(relativePath) ?? null;
        return createFile({
          path: relativePath,
          ast: fileArtifact.ast,
          headerLines: fileArtifact.headerLines,
          metadata: {
            ...(runtimeFile.metadata ?? {}),
            chunkFile: relativePath,
            chunkId,
            role: relativePath === targetFile ? "entry" : "module",
            ...(relativePath === targetFile ? {} : { generated: buildSelectedModuleLoweringMetadata() }),
            ...(modulePlan
              ? {
                  moduleExtraction: {
                    id: modulePlan.id,
                    kind: "atomic",
                    nameHint: modulePlan.nameHint,
                    ownerIds: [...modulePlan.ownerIds],
                    unitIds: [...modulePlan.unitIds],
                  },
                }
              : {}),
          },
          parserOptions: runtimeParserOptions,
        });
      }),
      metadata: {
        ...(chunk?.metadata ?? {}),
        moduleExtractionState: {
          analysis,
          atomicUnits: plan.atomicUnits.map(cloneAtomicUnit),
          currentModules,
          headerLines: [...runtimeHeaderLines],
          kind: "js.module_extraction_state",
          mode: "atomic",
          // Original (pre-lowering) AST kept here so later regrouping or
          // logical-materialization passes can cloneNode it and re-lower in
          // one cheap shot, instead of re-serializing + re-parsing the chunk
          // source every time.
          originalAst: runtimeFile.ast,
          parserOptions: runtimeParserOptions,
          runtimeFile: targetFile,
          targetDir: normalizeRelativeFile(targetDir),
        },
      },
    });
    setChunk(artifact, nextChunk);
    const writebackMs = durationMsSince(writebackStartedAt);

    const chunkManifest = getArtifactChunkManifest(artifact, chunkId);
    setArtifactChunkManifest(artifact, chunkId, {
      ...chunkManifest,
      atomicModules: {
        count: currentModules.length,
        moduleIds: currentModules.map((modulePlan) => modulePlan.id),
        selectedOwners: plan.selectedOwnerCount,
        targetDir: normalizeRelativeFile(targetDir),
      },
      entryFile: targetFile,
      selectedModuleLowerings: result.applied,
    });

    const report = {
      chunkId,
      counts: {
        applied: result.applied.length,
        atomicUnits: plan.atomicUnitCount,
        modules: currentModules.length,
        selectedOwners: plan.selectedOwnerCount,
      },
      timingsMs: {
        analysis: analysisMs,
        ...(plan.timingsMs
          ? {
              planBuildAtomicUnits: plan.timingsMs.buildAtomicUnits,
              planFinalizeAtomicUnits: plan.timingsMs.finalizeAtomicUnits,
              planFinalizeModules: plan.timingsMs.finalizeModules,
              planSelectOwners: plan.timingsMs.selectOwners,
            }
          : {}),
        parseLoweringAst: parseMs,
        plan: planningMs,
        total: durationMsSince(chunkStartedAt),
        writeback: writebackMs,
        lower: loweringMs,
      },
      moduleIds: currentModules.map((modulePlan) => modulePlan.id),
      outDir: resolvedReportOutDir ? relativeWorkspacePath(resolvedReportOutDir) : null,
    };
    reports.push(report);
    applied.push(...result.applied);
    if (resolvedReportOutDir) {
      writeJsonFile(join(resolvedReportOutDir, `${chunkId}.json`), report);
    }
    logProgress(
      `atomic-modules chunk=${chunkId} modules=${currentModules.length} analysis=${formatDuration(
        analysisMs
      )} plan=${formatDuration(planningMs)}${
        plan.timingsMs
          ? ` (select=${formatDuration(plan.timingsMs.selectOwners)} build=${formatDuration(
              plan.timingsMs.buildAtomicUnits
            )} finalizeUnits=${formatDuration(plan.timingsMs.finalizeAtomicUnits)} finalizeModules=${formatDuration(
              plan.timingsMs.finalizeModules
            )})`
          : ""
      } parse=${formatDuration(
        parseMs
      )} lower=${formatDuration(loweringMs)} writeback=${formatDuration(writebackMs)} total=${formatDuration(
        report.timingsMs.total
      )}`
    );
  }

  setArtifactManifest(artifact, {
    ...artifactManifest,
    atomicModules: {
      chunkCount: reports.length,
      moduleCount: reports.reduce((sum, report) => sum + report.counts.modules, 0),
    },
    counts: {
      ...(artifactManifest?.counts ?? {}),
      selectedModuleLowerings: applied.length,
    },
    selectedModuleLowerings: applied,
  });

  const manifest = {
    chunkCount: reports.length,
    chunks: reports,
    counts: {
      applied: applied.length,
      modules: reports.reduce((sum, report) => sum + report.counts.modules, 0),
    },
    durationMs: Number(process.hrtime.bigint() - startedAt) / 1_000_000,
    kind: "js.atomic_module_manifest",
    reportOutDir: resolvedReportOutDir ? relativeWorkspacePath(resolvedReportOutDir) : null,
    schemaVersion: 1,
  };
  if (resolvedReportSummaryPath) {
    writeJsonFile(resolvedReportSummaryPath, manifest);
  }

  logProgress(
    `atomic-modules done chunks=${reports.length} modules=${manifest.counts.modules} duration=${formatDurationSince(startedAt)}`
  );
  return {
    artifact,
    manifest,
  };
}

function cloneAtomicUnit(unit) {
  return {
    attachedItemIds: [...unit.attachedItemIds],
    ...(unit.bytes === null ? { bytes: null } : { bytes: unit.bytes }),
    id: unit.id,
    index: unit.index,
    lines: unit.lines,
    memberNames: [...unit.memberNames],
    ownerIds: [...unit.ownerIds],
    ownerFragments: cloneOwnerFragments(unit.ownerFragments),
    startOrdinal: unit.startOrdinal,
  };
}

function cloneModulePlan(modulePlan) {
  return {
    attachedItemIds: [...modulePlan.attachedItemIds],
    ...(modulePlan.bytes === null ? { bytes: null } : { bytes: modulePlan.bytes }),
    id: modulePlan.id,
    index: modulePlan.index,
    ...(modulePlan.initName ? { initName: modulePlan.initName } : {}),
    lines: modulePlan.lines,
    memberNames: [...modulePlan.memberNames],
    ...(modulePlan.modulePath ? { modulePath: modulePlan.modulePath } : {}),
    nameHint: modulePlan.nameHint,
    ownerIds: [...modulePlan.ownerIds],
    ownerFragments: cloneOwnerFragments(modulePlan.ownerFragments),
    startOrdinal: modulePlan.startOrdinal,
    ...(modulePlan.targetFile ? { targetFile: modulePlan.targetFile } : {}),
    unitIds: [...modulePlan.unitIds],
  };
}

function normalizeChunkIds(chunkIds) {
  if (!Array.isArray(chunkIds) || chunkIds.length === 0) {
    throw new Error("extractAtomicModules requires at least one chunkId");
  }
  return [...new Set(chunkIds.map(normalizeRelativeFile))];
}

function normalizeRelativeFile(value) {
  if (typeof value !== "string" || value === "") {
    throw new Error(`Expected a non-empty relative path, got: ${value}`);
  }
  const normalized = posix.normalize(value.split("\\").join("/"));
  if (normalized === "." || normalized.startsWith("/") || normalized.split("/").includes("..")) {
    throw new Error(`Invalid relative path: ${value}`);
  }
  return normalized;
}

function pruneArtifactToChunkIds(artifact, chunkIds) {
  const selectedChunkIds = new Set(chunkIds);
  removeFiles(artifact, (file) => {
    const chunkId = file.metadata?.chunkId ?? null;
    return chunkId !== null && !selectedChunkIds.has(chunkId);
  });
  const artifactManifest = getArtifactManifest(artifact);
  if (artifactManifest?.chunks) {
    setArtifactManifest(artifact, {
      ...artifactManifest,
      chunks: artifactManifest.chunks.filter((chunk) => selectedChunkIds.has(chunk.chunkId)),
    });
  }
  for (const chunk of artifactManifest?.chunks ?? []) {
    if (!selectedChunkIds.has(chunk.chunkId)) {
      deleteArtifactChunkManifest(artifact, chunk.chunkId);
    }
  }
}

function readBoundaryAnalysis(path) {
  const analysis = JSON.parse(readFileSync(path, "utf8"));
  if (analysis?.kind !== "js.runtime_boundary_analysis") {
    throw new Error(`Expected runtime boundary analysis at ${path}, got ${analysis?.kind ?? "unknown"}`);
  }
  return analysis;
}

function durationMsSince(startedAt) {
  return Number(process.hrtime.bigint() - startedAt) / 1_000_000;
}

function cloneOwnerFragments(ownerFragments) {
  return Array.isArray(ownerFragments)
    ? ownerFragments.map((fragment) => ({
        ...fragment,
        declaratorIndices: [...fragment.declaratorIndices],
        memberNames: [...fragment.memberNames],
      }))
    : [];
}

function formatDuration(durationMs) {
  return `${durationMs.toFixed(3)}ms`;
}
