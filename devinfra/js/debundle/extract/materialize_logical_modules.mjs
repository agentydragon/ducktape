import { existsSync, readFileSync } from "node:fs";
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
import { buildSelectedModuleLoweringMetadata, extractSelectedModulePlanInAst } from "./init_region.mjs";
import {
  buildLogicalModulePlans,
  closeSelectedOwnerIdsOverDependencyGraph,
  logicalSelectedOwnerIdsForChunk,
} from "./logical_modules.mjs";
import {
  defaultSelectedOwnerIdsForAnalysis,
  deriveSelectedModuleTarget,
  planSelectedAtomicModules,
} from "./planner.mjs";

export function materializeLogicalModules({
  artifact,
  boundaryAnalysisDir = undefined,
  chunkIds,
  file = undefined,
  pruneOtherChunks = true,
  force = false,
  operations = [],
  reportOutDir = undefined,
  reportSummaryPath = undefined,
  selectedOwnerIdsByChunkPath = undefined,
  selectedOwnerIdsByChunk = undefined,
  targetDir = "modules",
}) {
  requirePipelineArtifact(artifact, "materializeLogicalModules");
  let artifactManifest = getArtifactManifestOrDerived(artifact);

  const selectedChunkIds = normalizeChunkIds(chunkIds);
  const startedAt = process.hrtime.bigint();
  const resolvedBoundaryAnalysisDir = boundaryAnalysisDir ? resolveWorkspacePath(boundaryAnalysisDir) : null;
  const resolvedSelectedOwnerIdsByChunkPath = selectedOwnerIdsByChunkPath
    ? resolveWorkspacePath(selectedOwnerIdsByChunkPath)
    : null;
  const normalizedTargetDir = normalizeOptionalRelativeDir(targetDir);
  const reports = [];
  const applied = [];
  const selectedOwnerIdsCache = normalizeSelectedOwnerIdsCache(
    selectedOwnerIdsByChunk ?? readSelectedOwnerIdsCache(resolvedSelectedOwnerIdsByChunkPath)
  );

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

  logProgress(`logical-modules start chunks=${selectedChunkIds.length}`);

  for (const chunkId of selectedChunkIds) {
    const targetFile = file ? normalizeRelativeFile(file) : getChunkEntryPath(artifact, chunkId);
    if (!targetFile) {
      throw new Error(`materializeLogicalModules could not determine entry file for chunk: ${chunkId}`);
    }
    const runtimeFile = file ? getChunkFile(artifact, chunkId, targetFile) : getChunkEntryFile(artifact, chunkId);
    if (!runtimeFile?.ast) {
      throw new Error(`materializeLogicalModules missing entry AST for chunk: ${chunkId}`);
    }

    const runtimeHeaderLines = runtimeFile.headerLines ?? [];
    const runtimeParserOptions = runtimeFile.parserOptions ?? DEFAULT_PARSER_OPTIONS;
    const chunkStartedAt = process.hrtime.bigint();
    const analysisStartedAt = process.hrtime.bigint();
    const boundaryAnalysisPath = resolvedBoundaryAnalysisDir
      ? join(resolvedBoundaryAnalysisDir, `${chunkId}.json`)
      : null;
    const analysis =
      boundaryAnalysisPath && existsSync(boundaryAnalysisPath)
        ? readBoundaryAnalysis(boundaryAnalysisPath)
        : analyzeRuntimeBoundaryAst(runtimeFile.ast, {
            chunkId,
            manifestPath: `${chunkId}/manifest.json`,
            runtimePath: `${chunkId}/${targetFile}`,
            uiVersion: artifactManifest?.uiVersion ?? null,
          });
    if (boundaryAnalysisPath && !existsSync(boundaryAnalysisPath)) {
      writeJsonFile(boundaryAnalysisPath, analysis);
    }
    const analysisMs = durationMsSince(analysisStartedAt);
    logProgress(`logical-modules chunk=${chunkId} analysis done duration=${formatDuration(analysisMs)}`);
    const planningStartedAt = process.hrtime.bigint();
    const baseSelectionStartedAt = process.hrtime.bigint();
    const baseSelectedOwnerIds = selectedOwnerIdsCache?.[chunkId] ?? defaultSelectedOwnerIdsForAnalysis(analysis);
    if (selectedOwnerIdsCache && !selectedOwnerIdsCache[chunkId]) {
      selectedOwnerIdsCache[chunkId] = [...baseSelectedOwnerIds].sort();
    }
    const baseSelectionMs = durationMsSince(baseSelectionStartedAt);
    const logicalClaimSelectionStartedAt = process.hrtime.bigint();
    const logicalClaimOwnerIds = logicalSelectedOwnerIdsForChunk(operations, { analysis, chunkId });
    const logicalClaimSelectionMs = durationMsSince(logicalClaimSelectionStartedAt);
    // The auto-selected owner base and the logical-member claim set are both only seeds.
    // Before atomic planning, close the merged seed set over the full owner dependency graph
    // so later lowering never depends on a provider owner that stayed behind in the runtime shell.
    const closureStartedAt = process.hrtime.bigint();
    const selectedOwnerIds = closeSelectedOwnerIdsOverDependencyGraph(
      mergeSelectedOwnerIds(baseSelectedOwnerIds, logicalClaimOwnerIds),
      {
        analysis,
        callerName: `materializeLogicalModules chunk=${chunkId}`,
      }
    );
    const closureMs = durationMsSince(closureStartedAt);
    const atomicPlanningStartedAt = process.hrtime.bigint();
    const atomicPlan = planSelectedAtomicModules(
      {
        analysis,
        code: null,
        programBody: runtimeFile.ast.program.body,
      },
      {
        selectedOwnerIds,
      }
    );
    const atomicPlanningMs = durationMsSince(atomicPlanningStartedAt);
    const planningMs = durationMsSince(planningStartedAt);
    const atomicPlanTimings = atomicPlan.timingsMs ?? {};
    logProgress(
      `logical-modules chunk=${chunkId} planning done atomicUnits=${atomicPlan.atomicUnitCount} ` +
        `selectedOwners=${atomicPlan.selectedOwnerCount} duration=${formatDuration(planningMs)} ` +
        `baseSelection=${formatDuration(baseSelectionMs)} ` +
        `logicalClaims=${formatDuration(logicalClaimSelectionMs)} ` +
        `closure=${formatDuration(closureMs)} ` +
        `atomicPlanning=${formatDuration(atomicPlanningMs)} ` +
        `selectOwners=${formatDuration(atomicPlanTimings.selectOwners ?? 0)} ` +
        `buildAtomicUnits=${formatDuration(atomicPlanTimings.buildAtomicUnits ?? 0)} ` +
        `finalizeAtomicUnits=${formatDuration(atomicPlanTimings.finalizeAtomicUnits ?? 0)} ` +
        `finalizeModules=${formatDuration(atomicPlanTimings.finalizeModules ?? 0)}`
    );

    const atomicModules = atomicPlan.modulePlans.map((modulePlan, index) => {
      const target = deriveSelectedModuleTarget(modulePlan, index, { targetDir });
      return {
        ...cloneModulePlan(modulePlan),
        initName: target.init,
        targetFile: target.file,
      };
    });
    const logicalPlanStartedAt = process.hrtime.bigint();
    const logicalModules = buildLogicalModulePlans(atomicModules, operations, { analysis, chunkId, targetDir });
    const logicalPlanMs = durationMsSince(logicalPlanStartedAt);
    logProgress(
      `logical-modules chunk=${chunkId} logical-plan done final=${logicalModules.modules.length} explicit=${logicalModules.counts.explicitModules} residual=${logicalModules.counts.residualModules} duration=${formatDuration(logicalPlanMs)}`
    );

    const parseStartedAt = process.hrtime.bigint();
    const loweringAst = t.cloneNode(runtimeFile.ast, true);
    const parseMs = durationMsSince(parseStartedAt);
    logProgress(`logical-modules chunk=${chunkId} clone-ast done duration=${formatDuration(parseMs)}`);
    const loweringStartedAt = process.hrtime.bigint();
    const result = extractSelectedModulePlanInAst(
      loweringAst,
      {
        kind: "js.selected_module_plan",
        modulePlans: logicalModules.modules,
      },
      {
        analysis,
        chunkId,
        file: targetFile,
        headerLines: runtimeHeaderLines,
        idPrefix: "logical_module",
        targetDir,
      }
    );
    const loweringMs = durationMsSince(loweringStartedAt);
    logProgress(
      `logical-modules chunk=${chunkId} lowering done files=${result.jsFiles.size} applied=${result.applied.length} duration=${formatDuration(loweringMs)}`
    );

    const moduleByTargetFile = new Map(logicalModules.modules.map((modulePlan) => [modulePlan.targetFile, modulePlan]));
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
                    atomicBoundaryUnits:
                      modulePlan.atomicBoundaryUnits?.map((unit) => ({
                        attachedItemIds: [...unit.attachedItemIds],
                        id: unit.id,
                        memberNames: [...unit.memberNames],
                        ownerIds: [...unit.ownerIds],
                        ownerFragments: cloneOwnerFragments(unit.ownerFragments),
                        startOrdinal: unit.startOrdinal,
                        unitIds: [...unit.unitIds],
                      })) ?? [],
                    id: modulePlan.id,
                    kind: "logical",
                    nameHint: modulePlan.nameHint,
                    ownerIds: [...modulePlan.ownerIds],
                    ownerFragments: cloneOwnerFragments(modulePlan.ownerFragments),
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
          atomicUnits: atomicPlan.atomicUnits.map(cloneAtomicUnit),
          currentModules: logicalModules.modules.map(cloneModulePlan),
          headerLines: [...runtimeHeaderLines],
          kind: "js.module_extraction_state",
          mode: "logical",
          originalAst: runtimeFile.ast,
          parserOptions: runtimeParserOptions,
          runtimeFile: targetFile,
          sourceAtomicModules: atomicModules.map(cloneModulePlan),
          targetDir: normalizedTargetDir,
        },
      },
    });
    setChunk(artifact, nextChunk);
    const writebackMs = durationMsSince(writebackStartedAt);
    logProgress(`logical-modules chunk=${chunkId} writeback done duration=${formatDuration(writebackMs)}`);

    const chunkManifest = getArtifactChunkManifest(artifact, chunkId);
    setArtifactChunkManifest(artifact, chunkId, {
      ...chunkManifest,
      entryFile: targetFile,
      logicalModules: {
        count: logicalModules.modules.length,
        moduleIds: logicalModules.modules.map((modulePlan) => modulePlan.id),
        targetDir: normalizedTargetDir,
      },
      selectedModuleLowerings: result.applied,
    });

    const finalModules = logicalModules.modules.map((modulePlan) => ({
      atomicBoundaryUnits:
        modulePlan.atomicBoundaryUnits?.map((unit) => ({
          attachedItemIds: [...unit.attachedItemIds],
          id: unit.id,
          memberNames: [...unit.memberNames],
          ownerIds: [...unit.ownerIds],
          ownerFragments: cloneOwnerFragments(unit.ownerFragments),
          startOrdinal: unit.startOrdinal,
          unitIds: [...unit.unitIds],
        })) ?? [],
      file: modulePlan.targetFile,
      id: modulePlan.id,
      memberNames: [...modulePlan.memberNames],
      path: modulePlan.modulePath,
      ownerIds: [...modulePlan.ownerIds],
      ownerFragments: cloneOwnerFragments(modulePlan.ownerFragments),
      startOrdinal: modulePlan.startOrdinal,
      unitIds: [...modulePlan.unitIds],
    }));
    const report = {
      chunkId,
      counts: {
        applied: result.applied.length,
        atomicModules: atomicModules.length,
        atomicUnits: atomicPlan.atomicUnitCount,
        blockedMembers: logicalModules.counts.blockedMembers,
        explicitLogicalModules: logicalModules.counts.explicitModules,
        finalModules: logicalModules.counts.totalModules,
        residualLogicalModules: logicalModules.counts.residualModules,
        selectedOwners: atomicPlan.selectedOwnerCount,
        unmatchedMembers: logicalModules.counts.unmatchedMembers,
      },
      finalModuleContents: finalModules,
      requestedLogicalModules: logicalModules.reports,
      timingsMs: {
        analysis: analysisMs,
        ...(atomicPlan.timingsMs
          ? {
              planBuildAtomicUnits: atomicPlan.timingsMs.buildAtomicUnits,
              planFinalizeAtomicUnits: atomicPlan.timingsMs.finalizeAtomicUnits,
              planFinalizeModules: atomicPlan.timingsMs.finalizeModules,
              planSelectOwners: atomicPlan.timingsMs.selectOwners,
            }
          : {}),
        lower: loweringMs,
        parseLoweringAst: parseMs,
        plan: planningMs,
        total: durationMsSince(chunkStartedAt),
        writeback: writebackMs,
      },
    };
    reports.push(report);
    applied.push(...result.applied);
    if (resolvedReportOutDir) {
      writeJsonFile(join(resolvedReportOutDir, `${chunkId}.json`), report);
    }
    logProgress(
      `logical-modules chunk=${chunkId} final=${logicalModules.modules.length} explicit=${logicalModules.counts.explicitModules} residual=${logicalModules.counts.residualModules} analysis=${formatDuration(
        analysisMs
      )} plan=${formatDuration(planningMs)} parse=${formatDuration(parseMs)} lower=${formatDuration(
        loweringMs
      )} writeback=${formatDuration(writebackMs)} total=${formatDuration(report.timingsMs.total)}`
    );
  }

  setArtifactManifest(artifact, {
    ...artifactManifest,
    counts: {
      ...(artifactManifest?.counts ?? {}),
      selectedModuleLowerings: applied.length,
    },
    logicalModules: {
      chunkCount: reports.length,
      moduleCount: reports.reduce((sum, report) => sum + report.counts.finalModules, 0),
    },
    selectedModuleLowerings: applied,
  });

  const manifest = {
    chunkCount: reports.length,
    chunks: reports,
    counts: {
      applied: applied.length,
      blockedMembers: reports.reduce((sum, report) => sum + report.counts.blockedMembers, 0),
      finalModules: reports.reduce((sum, report) => sum + report.counts.finalModules, 0),
      explicitLogicalModules: reports.reduce((sum, report) => sum + report.counts.explicitLogicalModules, 0),
      residualLogicalModules: reports.reduce((sum, report) => sum + report.counts.residualLogicalModules, 0),
    },
    durationMs: Number(process.hrtime.bigint() - startedAt) / 1_000_000,
    kind: "js.logical_module_manifest",
    reportOutDir: resolvedReportOutDir ? relativeWorkspacePath(resolvedReportOutDir) : null,
    schemaVersion: 1,
  };
  if (resolvedReportSummaryPath) {
    writeJsonFile(resolvedReportSummaryPath, manifest);
  }
  if (resolvedSelectedOwnerIdsByChunkPath && selectedOwnerIdsCache) {
    writeJsonFile(resolvedSelectedOwnerIdsByChunkPath, {
      chunkOwnerIds: selectedOwnerIdsCache,
      kind: "js.selected_owner_ids_cache",
      schemaVersion: 1,
    });
  }

  logProgress(
    `logical-modules done chunks=${reports.length} modules=${manifest.counts.finalModules} duration=${formatDurationSince(startedAt)}`
  );
  return {
    artifact,
    manifest,
  };
}

function mergeSelectedOwnerIds(...ownerIdCollections) {
  const mergedOwnerIds = new Set();
  for (const ownerIds of ownerIdCollections) {
    if (!ownerIds) {
      continue;
    }
    for (const ownerId of ownerIds) {
      mergedOwnerIds.add(ownerId);
    }
  }
  return mergedOwnerIds;
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
    modulePath: modulePlan.modulePath,
    nameHint: modulePlan.nameHint,
    ownerIds: [...modulePlan.ownerIds],
    ownerFragments: cloneOwnerFragments(modulePlan.ownerFragments),
    ...(Array.isArray(modulePlan.bindingPlacements)
      ? {
          bindingPlacements: modulePlan.bindingPlacements.map((entry) => ({ ...entry })),
        }
      : {}),
    ...(Array.isArray(modulePlan.requestedBindings)
      ? {
          requestedBindings: modulePlan.requestedBindings.map((binding) => ({ ...binding })),
        }
      : {}),
    startOrdinal: modulePlan.startOrdinal,
    ...(modulePlan.targetFile ? { targetFile: modulePlan.targetFile } : {}),
    unitIds: [...modulePlan.unitIds],
  };
}

function normalizeChunkIds(chunkIds) {
  if (!Array.isArray(chunkIds) || chunkIds.length === 0) {
    throw new Error("materializeLogicalModules requires at least one chunkId");
  }
  return [...new Set(chunkIds.map(normalizeRelativeFile))];
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

function normalizeOptionalRelativeDir(value) {
  if (value === null || value === undefined) {
    return null;
  }
  return normalizeRelativeFile(value);
}

function pruneArtifactToChunkIds(artifact, chunkIds) {
  const selectedChunkIds = new Set(chunkIds);
  removeFiles(artifact, (fileArtifact) => {
    const chunkId = fileArtifact.metadata?.chunkId ?? null;
    return chunkId !== null && !selectedChunkIds.has(chunkId);
  });
  const artifactManifest = getArtifactManifestOrDerived(artifact);
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

function readSelectedOwnerIdsCache(path) {
  if (!path || !existsSync(path)) {
    return null;
  }
  const cache = JSON.parse(readFileSync(path, "utf8"));
  const chunkOwnerIds = cache?.chunkOwnerIds ?? cache;
  if (!chunkOwnerIds || typeof chunkOwnerIds !== "object") {
    throw new Error(`Expected selected owner id cache map at ${path}`);
  }
  return chunkOwnerIds;
}

function normalizeSelectedOwnerIdsCache(cache) {
  if (!cache || typeof cache !== "object") {
    return {};
  }
  return Object.fromEntries(
    Object.entries(cache).map(([chunkId, ownerIds]) => [chunkId, normalizeSelectedOwnerIdsForChunk(ownerIds, chunkId)])
  );
}

function normalizeSelectedOwnerIdsForChunk(ownerIds, chunkId) {
  if (ownerIds instanceof Set) {
    return [...ownerIds].sort();
  }
  if (Array.isArray(ownerIds)) {
    return [...ownerIds].sort();
  }
  throw new Error(`Expected selected owner ids for chunk ${chunkId} to be an array or Set`);
}

function durationMsSince(startedAt) {
  return Number(process.hrtime.bigint() - startedAt) / 1_000_000;
}

function formatDuration(durationMs) {
  return `${durationMs.toFixed(3)}ms`;
}
