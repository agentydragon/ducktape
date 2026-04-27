import { readFileSync } from "node:fs";
import { join, posix } from "node:path";
import { parse } from "@babel/parser";
import * as t from "@babel/types";
import { analyzeRuntimeBoundaryAst } from "../analysis/boundary.mjs";
import { DEFAULT_PARSER_OPTIONS, writeJsonFile } from "../common/parser_options.mjs";
import {
  deleteArtifactChunkManifest,
  createFile,
  getArtifactChunkManifest,
  getArtifactManifest,
  getChunkFile,
  getChunkEntryFile,
  getChunkEntryPath,
  removeFiles,
  requirePipelineArtifact,
  setArtifactChunkManifest,
  setArtifactManifest,
  setFile,
} from "../common/artifact.mjs";
import {
  formatDurationSince,
  logProgress,
  prepareOutputDir,
  relativeWorkspacePath,
  resolveWorkspacePath,
} from "../common/io.mjs";
import { serializeGeneratedJsFile } from "../split/chunk.mjs";
import { extractGuidedSelectedOwnerModulesInAst } from "./init_region.mjs";
import { planGuidedSelectedOwnerModules } from "./planner.mjs";

export function extractGuidedSelectedOwnerModules({
  artifact,
  boundaryAnalysisDir = undefined,
  chunkIds,
  file = undefined,
  targetDir = "modules",
  filePrefix = "guided_",
  idPrefix = "guided_selected_owner_module",
  initPrefix = "init_guided_",
  minModuleLines = 500,
  maxModuleLines = 20_000,
  pruneOtherChunks = true,
  force = false,
  reportOutDir = undefined,
  reportSummaryPath = undefined,
  selectedOwnerIdsByChunk = undefined,
}) {
  requirePipelineArtifact(artifact, "extractGuidedSelectedOwnerModules");
  let snapshotManifest = getArtifactManifest(artifact);
  if (!Array.isArray(snapshotManifest?.chunks)) {
    throw new Error("extractGuidedSelectedOwnerModules requires an artifact manifest in artifact extras");
  }

  const selectedChunkIds = normalizeChunkIds(chunkIds);
  const startedAt = process.hrtime.bigint();
  const reports = [];
  const applied = [];

  let resolvedReportOutDir = null;
  let resolvedReportSummaryPath = null;
  const resolvedBoundaryAnalysisDir = boundaryAnalysisDir ? resolveWorkspacePath(boundaryAnalysisDir) : null;
  if (reportOutDir) {
    resolvedReportOutDir = resolveWorkspacePath(reportOutDir);
    prepareOutputDir(resolvedReportOutDir, { force });
    resolvedReportSummaryPath = resolveWorkspacePath(
      reportSummaryPath ?? posix.join(reportOutDir, "summary.json")
    );
  }

  logProgress(
    `guided-selected-owner start chunks=${selectedChunkIds.length} minLines=${minModuleLines} maxLines=${maxModuleLines}`
  );
  if (pruneOtherChunks) {
    logProgress(`guided-selected-owner prune start mem=${formatHeapUsage()}`);
    pruneArtifactToChunkIds(artifact, selectedChunkIds);
    snapshotManifest = getArtifactManifest(artifact);
    logProgress(`guided-selected-owner prune done mem=${formatHeapUsage()}`);
  }

  for (const chunkId of selectedChunkIds) {
    const targetFile = file
      ? normalizeRelativeFile(file)
      : getChunkEntryPath(artifact, chunkId);
    if (!targetFile) {
      throw new Error(`extractGuidedSelectedOwnerModules could not determine entry file for chunk: ${chunkId}`);
    }
    const runtimeFile = file
      ? getChunkFile(artifact, chunkId, targetFile)
      : getChunkEntryFile(artifact, chunkId);
    if (!runtimeFile?.ast) {
      throw new Error(`extractGuidedSelectedOwnerModules missing entry AST for chunk: ${chunkId}`);
    }
    const chunkManifest = getArtifactChunkManifest(artifact, chunkId);
    const codeBefore = serializeGeneratedJsFile(runtimeFile);
    const runtimeHeaderLines = runtimeFile.headerLines ?? [];
    const runtimeParserOptions = runtimeFile.parserOptions ?? DEFAULT_PARSER_OPTIONS;
    logProgress(`guided-selected-owner load-analysis chunk=${chunkId} mem=${formatHeapUsage()}`);
    const analysis =
      resolvedBoundaryAnalysisDir
        ? readBoundaryAnalysis(join(resolvedBoundaryAnalysisDir, `${chunkId}.json`))
        : analyzeRuntimeBoundaryAst(runtimeFile.ast, {
            chunkId,
            manifestPath: `${chunkId}/manifest.json`,
            runtimePath: `${chunkId}/${targetFile}`,
            uiVersion: snapshotManifest.uiVersion ?? null,
          });
    const itemMetricsById = buildItemMetricsById(analysis.programItems, runtimeFile.ast.program.body, codeBefore);
    logProgress(`guided-selected-owner analysis-ready chunk=${chunkId} mem=${formatHeapUsage()}`);
    runtimeFile.ast = undefined;
    runtimeFile.content = codeBefore;
    logProgress(`guided-selected-owner ast-released chunk=${chunkId} mem=${formatHeapUsage()}`);
    const plan = planGuidedSelectedOwnerModules(
      {
        analysis,
        code: codeBefore,
        itemMetricsById,
      },
      {
        maxModuleLines,
        minModuleLines,
        selectedOwnerIds: selectedOwnerIdsByChunk?.[chunkId] ?? null,
      }
    );
    logProgress(
      `guided-selected-owner plan chunk=${chunkId} owners=${plan.selectedOwnerCount} atomicUnits=${plan.atomicUnitCount} modules=${plan.modulePlans.length} mem=${formatHeapUsage()}`
    );
    const loweringAst = parse(codeBefore, runtimeParserOptions);
    logProgress(`guided-selected-owner ast-reparsed chunk=${chunkId} mem=${formatHeapUsage()}`);
    const result = extractGuidedSelectedOwnerModulesInAst(loweringAst, plan, {
      analysis,
      chunkId,
      file: targetFile,
      filePrefix,
      headerLines: runtimeHeaderLines,
      idPrefix,
      initPrefix,
      targetDir,
    });
    logProgress(
      `guided-selected-owner lowered chunk=${chunkId} applied=${result.applied.length} files=${result.jsFiles.size} mem=${formatHeapUsage()}`
    );

    for (const [relativePath, fileArtifact] of result.jsFiles.entries()) {
      setFile(
        artifact,
        createFile({
          path: relativePath,
          ast: fileArtifact.ast,
          headerLines: fileArtifact.headerLines,
          parserOptions: runtimeFile.parserOptions,
          metadata: {
            ...runtimeFile.metadata,
            chunkFile: relativePath,
            chunkId,
            role: relativePath === targetFile ? "entry" : "guided_module",
          },
        })
      );
    }

    const mergedOrderedInitExtractions = mergeRecordsById(chunkManifest?.orderedInitExtractions, result.applied);
    setArtifactChunkManifest(artifact, chunkId, {
      ...chunkManifest,
      guidedSelectedOwnerModules: {
        applied: result.applied.length,
        atomicUnits: plan.atomicUnitCount,
        maxModuleLines,
        minModuleLines,
        selectedOwners: plan.selectedOwnerCount,
      },
      orderedInitExtractions: mergedOrderedInitExtractions,
    });

    const report = buildChunkGuidedModuleReport({
      applied: result.applied,
      chunkId,
      codeBefore,
      file: targetFile,
      plan,
      targetDir,
      transformedFiles: result.jsFiles,
    });
    logProgress(
      `guided-selected-owner report chunk=${chunkId} modules=${report.counts.modules} removed=${report.bytes.removedFromRuntime} mem=${formatHeapUsage()}`
    );
    reports.push(report);
    applied.push(...result.applied);

    if (resolvedReportOutDir) {
      writeJsonFile(join(resolvedReportOutDir, `${chunkId}.json`), report);
    }
  }

  setArtifactManifest(artifact, {
    ...snapshotManifest,
    counts: {
      ...snapshotManifest.counts,
      orderedInitExtractions: mergeRecordsById(snapshotManifest.orderedInitExtractions, applied).length,
    },
    guidedSelectedOwnerModules: {
      chunkCount: reports.length,
      moduleCount: reports.reduce((sum, report) => sum + report.counts.modules, 0),
      removedRuntimeBytes: reports.reduce((sum, report) => sum + report.bytes.removedFromRuntime, 0),
    },
    orderedInitExtractions: mergeRecordsById(snapshotManifest.orderedInitExtractions, applied),
  });

  const manifest = buildGuidedModuleManifest({
    applied,
    chunkIds: selectedChunkIds,
    durationMs: Number(process.hrtime.bigint() - startedAt) / 1_000_000,
    reportOutDir: resolvedReportOutDir,
    reports,
  });

  if (resolvedReportSummaryPath) {
    writeJsonFile(resolvedReportSummaryPath, manifest);
  }

  logProgress(
    `guided-selected-owner done chunks=${reports.length} modules=${manifest.counts.modules} duration=${formatDurationSince(
      startedAt
    )}`
  );

  return {
    artifact,
    manifest,
  };
}

function buildGuidedModuleManifest({ applied, chunkIds, durationMs, reportOutDir, reports }) {
  return {
    schemaVersion: 1,
    kind: "js.guided_selected_owner_module_manifest",
    counts: {
      applied: applied.length,
      chunks: chunkIds.length,
      modules: reports.reduce((sum, report) => sum + report.counts.modules, 0),
    },
    durationMs,
    reportOutDir: reportOutDir ? relativeWorkspacePath(reportOutDir) : null,
    chunks: reports.map((report) => ({
      atomicUnits: report.counts.atomicUnits,
      chunkId: report.chunkId,
      modules: report.counts.modules,
      removedRuntimeBytes: report.bytes.removedFromRuntime,
      runtimeAfterBytes: report.bytes.runtimeAfter,
      runtimeBeforeBytes: report.bytes.runtimeBefore,
      selectedOwners: report.counts.selectedOwners,
    })),
  };
}

function buildChunkGuidedModuleReport({ applied, chunkId, codeBefore, file, plan, targetDir, transformedFiles }) {
  const runtimeFile = transformedFiles.get(normalizeRelativeFile(file));
  const runtimeAfter = serializeGeneratedJsFile(runtimeFile);
  const moduleEntries = [...transformedFiles.entries()]
    .filter(([relativePath]) => relativePath.startsWith(`${normalizeRelativeFile(targetDir)}/`))
    .map(([relativePath, fileArtifact]) => {
      const code = serializeGeneratedJsFile(fileArtifact);
      const topLevelKinds = fileArtifact.ast.program.body.map((node) => node.type);
      validateGeneratedModuleTopLevel(relativePath, fileArtifact.ast.program.body);
      return {
        file: relativePath,
        bytes: Buffer.byteLength(code),
        lineCount: code.split("\n").length,
        topLevelKinds,
        unresolvedImportSources: collectRelativeImports(fileArtifact.ast, relativePath),
      };
    })
    .sort((left, right) => right.bytes - left.bytes || left.file.localeCompare(right.file));

  const moduleFileSet = new Set(moduleEntries.map((entry) => entry.file));
  for (const entry of moduleEntries) {
    entry.importSources = entry.unresolvedImportSources.filter((source) => moduleFileSet.has(source));
    entry.importCount = entry.importSources.length;
    delete entry.unresolvedImportSources;
  }

  const importGraph = new Map(moduleEntries.map((entry) => [entry.file, entry.importSources]));
  const importSccs = stronglyConnectedComponents(
    moduleEntries.map((entry) => entry.file),
    (node) => importGraph.get(node) ?? []
  )
    .filter((component) => component.length > 1)
    .sort((left, right) => right.length - left.length || left[0].localeCompare(right[0]));

  const removedFromRuntime = Math.max(0, Buffer.byteLength(codeBefore) - Buffer.byteLength(runtimeAfter));
  return {
    schemaVersion: 1,
    kind: "js.guided_selected_owner_chunk_report",
    chunkId,
    counts: {
      applied: applied.length,
      atomicUnits: plan.atomicUnitCount,
      modules: moduleEntries.length,
      selectedOwners: plan.selectedOwnerCount,
    },
    bytes: {
      generatedModules: moduleEntries.reduce((sum, entry) => sum + entry.bytes, 0),
      removedFromRuntime,
      runtimeAfter: Buffer.byteLength(runtimeAfter),
      runtimeBefore: Buffer.byteLength(codeBefore),
    },
    moduleLineStats: summarizeLineCounts(moduleEntries.map((entry) => entry.lineCount)),
    importSccs: {
      count: importSccs.length,
      largest: importSccs[0]?.length ?? 0,
      sizes: importSccs.map((component) => component.length),
    },
    modules: moduleEntries,
    operationTargets: moduleEntries.map((entry) => entry.file),
  };
}

function summarizeLineCounts(lineCounts) {
  if (lineCounts.length === 0) {
    return { max: 0, median: 0, min: 0 };
  }
  const sorted = [...lineCounts].sort((left, right) => left - right);
  return {
    max: sorted.at(-1),
    median: sorted[Math.floor(sorted.length / 2)],
    min: sorted[0],
  };
}

function collectRelativeImports(ast, relativePath) {
  const imports = [];
  for (const statement of ast.program.body) {
    if (!t.isImportDeclaration(statement)) {
      continue;
    }
    const source = statement.source.value;
    if (typeof source !== "string" || !source.startsWith(".")) {
      continue;
    }
    const resolved = posix.normalize(posix.join(posix.dirname(relativePath), source));
    imports.push(resolved);
  }
  return imports.sort();
}

function validateGeneratedModuleTopLevel(relativePath, body) {
  for (const statement of body) {
    if (t.isImportDeclaration(statement)) {
      continue;
    }
    if (t.isExportNamedDeclaration(statement)) {
      continue;
    }
    throw new Error(
      `Guided selected-owner module ${relativePath} emitted unsupported top-level node ${statement.type}`
    );
  }
}

function normalizeChunkIds(chunkIds) {
  if (!Array.isArray(chunkIds) || chunkIds.length === 0) {
    throw new Error("extractGuidedSelectedOwnerModules requires at least one chunkId");
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

function mergeRecordsById(existing = [], appended = []) {
  const merged = new Map();
  for (const record of [...(existing ?? []), ...(appended ?? [])]) {
    merged.set(record.id, record);
  }
  return [...merged.values()];
}

function pruneArtifactToChunkIds(artifact, chunkIds) {
  const selectedChunkIds = new Set(chunkIds);
  removeFiles(artifact, (file) => {
    const chunkId = file.metadata?.chunkId ?? null;
    return chunkId !== null && !selectedChunkIds.has(chunkId);
  });
  const snapshotManifest = getArtifactManifest(artifact);
  if (snapshotManifest?.chunks) {
    setArtifactManifest(artifact, {
      ...snapshotManifest,
      chunks: snapshotManifest.chunks.filter((chunk) => selectedChunkIds.has(chunk.chunkId)),
    });
  }
  for (const chunk of snapshotManifest?.chunks ?? []) {
    if (!selectedChunkIds.has(chunk.chunkId)) {
      deleteArtifactChunkManifest(artifact, chunk.chunkId);
    }
  }
}

function buildItemMetricsById(programItems, programBody, code) {
  const metrics = new Map();
  for (const item of programItems ?? []) {
    const statement = programBody[item.ordinal];
    metrics.set(item.id, {
      bytes:
        typeof statement?.start === "number" && typeof statement?.end === "number"
          ? Buffer.byteLength(code.slice(statement.start, statement.end))
          : 0,
      lines:
        statement?.loc
          ? statement.loc.end.line - statement.loc.start.line + 1
          : 0,
    });
  }
  return metrics;
}

function readBoundaryAnalysis(path) {
  const analysis = JSON.parse(readFileSync(path, "utf8"));
  if (analysis?.kind !== "js.runtime_boundary_analysis") {
    throw new Error(`Expected runtime boundary analysis at ${path}, got ${analysis?.kind ?? "unknown"}`);
  }
  return analysis;
}

function stronglyConnectedComponents(nodes, adjacencyFor) {
  let index = 0;
  const indexes = new Map();
  const lowLinks = new Map();
  const stack = [];
  const onStack = new Set();
  const components = [];

  for (const node of nodes) {
    if (!indexes.has(node)) {
      visit(node);
    }
  }

  return components;

  function visit(node) {
    indexes.set(node, index);
    lowLinks.set(node, index);
    index += 1;
    stack.push(node);
    onStack.add(node);

    for (const dependency of adjacencyFor(node)) {
      if (!indexes.has(dependency)) {
        visit(dependency);
        lowLinks.set(node, Math.min(lowLinks.get(node), lowLinks.get(dependency)));
      } else if (onStack.has(dependency)) {
        lowLinks.set(node, Math.min(lowLinks.get(node), indexes.get(dependency)));
      }
    }

    if (lowLinks.get(node) !== indexes.get(node)) {
      return;
    }

    const component = [];
    for (;;) {
      const value = stack.pop();
      onStack.delete(value);
      component.push(value);
      if (value === node) {
        break;
      }
    }
    components.push(component.sort());
  }
}

function formatHeapUsage() {
  const { heapUsed, rss } = process.memoryUsage();
  return `heap=${formatMiB(heapUsed)} rss=${formatMiB(rss)}`;
}

function formatMiB(bytes) {
  return `${(bytes / (1024 * 1024)).toFixed(1)}MiB`;
}
