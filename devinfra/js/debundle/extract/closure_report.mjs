import { mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { analyzeRuntimeBoundaryAst } from "../analysis/boundary.mjs";
import {
  getArtifactManifest,
  getChunkEntryFile,
  getChunkEntryPath,
  requirePipelineArtifact,
} from "../common/artifact.mjs";
import { writeJsonFile } from "../common/parser_options.mjs";
import {
  formatDurationSince,
  logProgress,
  prepareOutputDir,
  relativeWorkspacePath,
  requireValue,
  resolveWorkspacePath,
} from "../common/io.mjs";
import { serializeGeneratedJsFile } from "../split/chunk.mjs";
import { packOrderedInitOwnerClosures, planOrderedInitOwnerClosureExtractions } from "./decl_graph.mjs";

export function parseOwnerClosurePlanReportArgs(argv) {
  const options = {
    chunkIds: [],
    force: false,
    help: false,
    inputManifestPath: undefined,
    inputRoot: undefined,
    limit: 200,
    lowering: "staged_shell",
    outDir: undefined,
    summaryPath: undefined,
    uiVersion: undefined,
  };

  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    switch (arg) {
      case "--chunk-id":
        options.chunkIds.push(requireValue(argv, ++index, arg));
        break;
      case "--force":
        options.force = true;
        break;
      case "--help":
      case "-h":
        options.help = true;
        break;
      case "--input-manifest":
      case "--manifest":
        options.inputManifestPath = resolveWorkspacePath(requireValue(argv, ++index, arg));
        break;
      case "--input-root":
        options.inputRoot = resolveWorkspacePath(requireValue(argv, ++index, arg));
        break;
      case "--limit":
        options.limit = parsePositiveInteger(requireValue(argv, ++index, arg), arg);
        break;
      case "--out":
        options.outDir = resolveWorkspacePath(requireValue(argv, ++index, arg));
        break;
      case "--summary":
        options.summaryPath = resolveWorkspacePath(requireValue(argv, ++index, arg));
        break;
      case "--ui-version":
        options.uiVersion = requireValue(argv, ++index, arg);
        break;
      default:
        throw new Error(`Unknown argument: ${arg}`);
    }
  }

  if (options.help) {
    return options;
  }
  if (!options.inputRoot) {
    throw new Error("--input-root is required");
  }
  if (!options.outDir) {
    throw new Error("--out is required");
  }
  return options;
}

function parsePositiveInteger(value, flag) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isSafeInteger(parsed) || parsed < 1 || String(parsed) !== value) {
    throw new Error(`${flag} requires a positive integer`);
  }
  return parsed;
}

export function extractOwnerClosurePlanReport(options) {
  const artifact = requirePipelineArtifact(options.artifact, "extractOwnerClosurePlanReport");
  const inputRoot = options.inputRoot ? resolve(options.inputRoot) : null;
  const outDir = resolve(options.outDir);
  const inputManifestPath = options.inputManifestPath ? resolve(options.inputManifestPath) : null;
  const summaryPath = resolve(options.summaryPath ?? join(dirname(outDir), "owner-closure-plan-summary.json"));

  prepareOutputDir(outDir, { force: options.force });

  const manifest = getArtifactManifest(artifact);
  if (!Array.isArray(manifest?.chunks)) {
    throw new Error("extractOwnerClosurePlanReport requires an artifact manifest in artifact extras");
  }
  const selectedChunks = selectChunks(manifest.chunks, options.chunkIds);

  logProgress(
    `owner-closure-report start chunks=${selectedChunks.length} lowering=${options.lowering ?? "staged_shell"} out=${relativeWorkspacePath(
      outDir
    )}`
  );
  const startedAt = process.hrtime.bigint();

  const chunkSummaries = [];
  for (const chunk of selectedChunks) {
    const entryFile = getChunkEntryFile(artifact, chunk.chunkId);
    const entryRelativePath = getChunkEntryPath(artifact, chunk.chunkId);
    if (!entryFile?.ast || !entryRelativePath) {
      throw new Error(`extractOwnerClosurePlanReport missing entry AST for chunk: ${chunk.chunkId}`);
    }
    const code = entryFile.ast ? serializeGeneratedJsFile(entryFile) : (entryFile.content ?? "");
    const analysis = analyzeRuntimeBoundaryAst(entryFile.ast, {
      chunkId: chunk.chunkId,
      manifestPath: describeArtifactChunkPath(inputRoot, chunk.chunkId, "manifest.json"),
      runtimePath: describeArtifactChunkPath(inputRoot, chunk.chunkId, entryRelativePath),
      uiVersion: options.uiVersion ?? manifest.uiVersion ?? null,
    });
    const plan = planOrderedInitOwnerClosureExtractions(analysis, {
      includeReportDetails: true,
    });
    const packed = packOrderedInitOwnerClosures(plan);
    const report = buildChunkReport({
      analysis,
      ast: entryFile.ast,
      chunk,
      code,
      inputManifestPath,
      inputRoot,
      manifest,
      options,
      outDir,
      packed,
      plan,
    });
    writeChunkReport(outDir, report);
    chunkSummaries.push(chunkSummaryFromReport(report));
  }

  const summary = buildSummary({
    chunkSummaries,
    inputManifestPath,
    inputRoot,
    manifest,
    options,
    outDir,
    summaryPath,
  });
  writeJsonFile(summaryPath, summary);
  logProgress(
    `owner-closure-report done chunks=${chunkSummaries.length} duration=${formatDurationSince(
      startedAt
    )} summary=${relativeWorkspacePath(summaryPath)}`
  );
  return summary;
}

function buildChunkReport({ analysis, ast, chunk, code, inputManifestPath, inputRoot, manifest, options, outDir, packed, plan }) {
  const statementMetricsByItemId = buildStatementMetrics(analysis, ast.program.body, code);
  const ownerOrdinalById = new Map(analysis.owners.map((owner) => [owner.id, owner.ordinal]));
  const closurePlans = plan.closurePlans.map((closurePlan) =>
    finalizeClosurePlanReport(closurePlan, statementMetricsByItemId, ownerOrdinalById, code)
  );
  const candidateBatchPlans = (packed.candidateBatchPlans ?? []).map((batchPlan) =>
    finalizeBatchPlanReport(batchPlan, statementMetricsByItemId, ownerOrdinalById, code)
  );
  const selectedBatchPlans = packed.batchPlans.map((batchPlan) =>
    finalizeBatchPlanReport(batchPlan, statementMetricsByItemId, ownerOrdinalById, code)
  );
  const limit = options.limit ?? 200;

  const selectedSemanticStatementBytes = selectedBatchPlans.reduce(
    (sum, batchPlan) => sum + batchPlan.semanticStatementBytes,
    0
  );
  const selectedLoweredRuntimeBytes = selectedBatchPlans.reduce(
    (sum, batchPlan) => sum + batchPlan.loweredRuntimeBytes,
    0
  );
  const reportPath = `${chunk.chunkId}.json`;

  return {
    schemaVersion: 1,
    kind: "js.ordered_init_owner_closure_report",
    uiVersion: options.uiVersion ?? manifest.uiVersion ?? null,
    chunkId: chunk.chunkId,
    inputRoot: inputRoot ? relativeWorkspacePath(inputRoot) : null,
    inputManifestPath: inputManifestPath ? relativeWorkspacePath(inputManifestPath) : null,
    manifestPath: analysis.manifestPath,
    runtimePath: analysis.runtimePath,
    outPath: relativeWorkspacePath(join(outDir, reportPath)),
    lowering: packed.lowering,
    limit,
    estimation: {
      semanticStatementBytes:
        "Exact UTF-8 bytes of declaration statements owned by the semantic closure. Does not include later lowering glue or unrelated runtime statements.",
      loweredRuntimeBytes:
        "Exact UTF-8 bytes of the selected declaration statements and attached replayable side effects removed from the runtime for staged-shell lowering.",
      approxRemainingRuntimeBytes:
        "runtimeBytes - selectedLoweredRuntimeBytes. This ignores new import/init stub bytes, so treat it as a lower-bound shell estimate.",
    },
    runtimeBytes: Buffer.byteLength(code),
    counts: {
      components: plan.componentPlans.length,
      closurePlans: closurePlans.length,
      candidateBatchPlans: candidateBatchPlans.length,
      lowerableBatchPlans: candidateBatchPlans.filter((batchPlan) => batchPlan.blockingReasons.length === 0).length,
      owners: analysis.owners.length,
      selectedBatchPlans: selectedBatchPlans.length,
    },
    truncation: {
      emittedClosurePlans: Math.min(limit, closurePlans.length),
      emittedCandidateBatchPlans: Math.min(limit, candidateBatchPlans.length),
      emittedSelectedBatchPlans: Math.min(limit, selectedBatchPlans.length),
    },
    totals: {
      approxRemainingRuntimeBytes: Math.max(0, Buffer.byteLength(code) - selectedLoweredRuntimeBytes),
      selectedLoweredRuntimeBytes,
      selectedSemanticStatementBytes,
    },
    closurePlans: [...closurePlans].sort(compareClosurePlanReports).slice(0, limit),
    candidateBatchPlans: [...candidateBatchPlans].sort(compareBatchPlanReports).slice(0, limit),
    selectedBatchPlans: [...selectedBatchPlans].sort(compareBatchPlanReports).slice(0, limit),
  };
}

function finalizeClosurePlanReport(plan, statementMetricsByItemId, ownerOrdinalById, code) {
  const semanticStatementBytes = exactStatementBytesForItems(plan.ownerIds, statementMetricsByItemId);
  const contiguousEnvelopeBytes = exactContiguousEnvelopeBytesForOwners(plan.contiguousEnvelopeOwnerIds, statementMetricsByItemId, code);
  const envelopeAddedOwnerStatementBytes = exactStatementBytesForItems(plan.envelopeAddedOwnerIds, statementMetricsByItemId);

  return {
    id: plan.id,
    seedComponentId: plan.seedComponentId,
    seedMemberNames: [...plan.seedMemberNames],
    semanticOwnerCount: plan.ownerIds.length,
    semanticOwnerOrdinalRanges: compactOwnerOrdinals(plan.ownerIds, ownerOrdinalById),
    semanticMemberNamePreview: previewNames(plan.memberNames),
    semanticMemberNameCount: plan.memberNames.length,
    semanticBlockingReasons: [...plan.blockingReasons],
    semanticStatementBytes,
    requiredClosureComponentCount: plan.requiredClosureComponentIds.length,
    requiredClosureComponentPreview: plan.requiredClosureComponentIds.slice(0, 16),
    directDependencyComponentCount: plan.directDependencyComponentIds.length,
    directDependencyComponentPreview: plan.directDependencyComponentIds.slice(0, 16),
    contiguousEnvelopeOwnerCount: plan.contiguousEnvelopeOwnerIds.length,
    contiguousEnvelopeOrdinalRange: {
      start: plan.contiguousEnvelopeStartOrdinal,
      end: plan.contiguousEnvelopeEndOrdinal,
    },
    contiguousEnvelopeMemberNamePreview: previewNames(plan.contiguousEnvelopeMemberNames),
    contiguousEnvelopeMemberNameCount: plan.contiguousEnvelopeMemberNames.length,
    contiguousEnvelopeBlockingReasons: [...plan.contiguousEnvelopeBlockingReasons],
    contiguousEnvelopeBytes,
    contiguousEnvelopeEstimatedSize: plan.contiguousEnvelopeEstimatedSize,
    envelopeAddedOwnerCount: plan.envelopeAddedOwnerIds.length,
    envelopeAddedOwnerOrdinalRanges: compactOwnerOrdinals(plan.envelopeAddedOwnerIds, ownerOrdinalById),
    envelopeAddedMemberNamePreview: previewNames(plan.envelopeAddedMemberNames),
    envelopeAddedMemberNameCount: plan.envelopeAddedMemberNames.length,
    envelopeAddedOwnerStatementBytes,
    approxEnvelopeExpansionBytes: Math.max(0, contiguousEnvelopeBytes - semanticStatementBytes),
  };
}

function finalizeBatchPlanReport(batchPlan, statementMetricsByItemId, ownerOrdinalById, code) {
  const semanticStatementBytes = exactStatementBytesForItems(batchPlan.semanticOwnerIds, statementMetricsByItemId);
  const loweredRuntimeBytes =
    batchPlan.lowering === "staged_shell"
      ? exactStatementBytesForItems(
          [...new Set([...(batchPlan.ownerIds ?? []), ...(batchPlan.attachedItemIds ?? [])])],
          statementMetricsByItemId
        )
      : exactContiguousEnvelopeBytesForOwners(batchPlan.ownerIds, statementMetricsByItemId, code);

  return {
    id: batchPlan.id,
    lowering: batchPlan.lowering,
    seedMemberNames: [...batchPlan.seedMemberNames],
    semanticOwnerCount: batchPlan.semanticOwnerIds.length,
    semanticOwnerOrdinalRanges: compactOwnerOrdinals(batchPlan.semanticOwnerIds, ownerOrdinalById),
    semanticMemberNamePreview: previewNames(batchPlan.semanticMemberNames),
    semanticMemberNameCount: batchPlan.semanticMemberNames.length,
    semanticStatementBytes,
    loweredOwnerCount: batchPlan.ownerIds.length,
    loweredOwnerOrdinalRange: compactContiguousOrdinalRange(batchPlan.ownerIds, ownerOrdinalById),
    loweredMemberNamePreview: previewNames(batchPlan.memberNames),
    loweredMemberNameCount: batchPlan.memberNames.length,
    loweredRuntimeBytes,
    blockingReasons: [...batchPlan.blockingReasons],
    envelopeAddedOwnerCount: batchPlan.envelopeAddedOwnerIds.length,
    envelopeAddedOwnerOrdinalRanges: compactOwnerOrdinals(batchPlan.envelopeAddedOwnerIds, ownerOrdinalById),
    envelopeAddedMemberNamePreview: previewNames(batchPlan.envelopeAddedMemberNames),
    envelopeAddedMemberNameCount: batchPlan.envelopeAddedMemberNames.length,
    attachedItemCount: batchPlan.attachedItemIds?.length ?? 0,
    stageCount: Array.isArray(batchPlan.stageRuns) ? batchPlan.stageRuns.length : 1,
    stageOrdinalRanges: (batchPlan.stageRuns ?? []).map((stageRun) => ({
      start: stageRun.startOrdinal,
      end: stageRun.endOrdinal,
    })),
    shellItemCount: batchPlan.shellItemIds?.length ?? 0,
    shellItemPreview: (batchPlan.shellItemIds ?? []).slice(0, 16),
    approxEnvelopeExpansionBytes: Math.max(0, loweredRuntimeBytes - semanticStatementBytes),
  };
}

function buildStatementMetrics(analysis, programBody, code) {
  const metricsByItemId = new Map();
  for (const item of analysis.programItems) {
    if (item.kind !== "declaration" && item.kind !== "side_effect") {
      continue;
    }
    const statement = programBody[item.ordinal];
    if (typeof statement?.start !== "number" || typeof statement?.end !== "number") {
      continue;
    }
    metricsByItemId.set(item.id, {
      start: statement.start,
      end: statement.end,
      bytes: Buffer.byteLength(code.slice(statement.start, statement.end)),
    });
  }
  return metricsByItemId;
}

function exactStatementBytesForItems(itemIds, statementMetricsByItemId) {
  return [...new Set(itemIds)]
    .map((itemId) => statementMetricsByItemId.get(itemId)?.bytes ?? 0)
    .reduce((sum, bytes) => sum + bytes, 0);
}

function exactContiguousEnvelopeBytesForOwners(ownerIds, statementMetricsByItemId, code) {
  const metrics = [...new Set(ownerIds)].map((ownerId) => statementMetricsByItemId.get(ownerId)).filter(Boolean);
  if (metrics.length === 0) {
    return 0;
  }
  const start = Math.min(...metrics.map((metric) => metric.start));
  const end = Math.max(...metrics.map((metric) => metric.end));
  return Buffer.byteLength(code.slice(start, end));
}

function writeChunkReport(outDir, report) {
  const outputPath = join(outDir, `${report.chunkId}.json`);
  mkdirSync(dirname(outputPath), { recursive: true });
  writeJsonFile(outputPath, report);
}

function chunkSummaryFromReport(report) {
  return {
    chunkId: report.chunkId,
    reportPath: report.outPath,
    runtimeBytes: report.runtimeBytes,
    counts: report.counts,
    totals: report.totals,
  };
}

function buildSummary({ chunkSummaries, inputManifestPath, inputRoot, manifest, options, outDir, summaryPath }) {
  return {
    schemaVersion: 1,
    kind: "js.ordered_init_owner_closure_report_summary",
    uiVersion: options.uiVersion ?? manifest.uiVersion ?? null,
    inputRoot: inputRoot ? relativeWorkspacePath(inputRoot) : null,
    inputManifestPath: inputManifestPath ? relativeWorkspacePath(inputManifestPath) : null,
    outDir: relativeWorkspacePath(outDir),
    summaryPath: relativeWorkspacePath(summaryPath),
    limit: options.limit ?? 200,
    lowering: options.lowering ?? "staged_shell",
    counts: {
      chunks: chunkSummaries.length,
      closurePlans: chunkSummaries.reduce((sum, chunk) => sum + chunk.counts.closurePlans, 0),
      candidateBatchPlans: chunkSummaries.reduce((sum, chunk) => sum + chunk.counts.candidateBatchPlans, 0),
      lowerableBatchPlans: chunkSummaries.reduce((sum, chunk) => sum + chunk.counts.lowerableBatchPlans, 0),
      owners: chunkSummaries.reduce((sum, chunk) => sum + chunk.counts.owners, 0),
      selectedBatchPlans: chunkSummaries.reduce((sum, chunk) => sum + chunk.counts.selectedBatchPlans, 0),
    },
    totals: {
      approxRemainingRuntimeBytes: chunkSummaries.reduce(
        (sum, chunk) => sum + chunk.totals.approxRemainingRuntimeBytes,
        0
      ),
      runtimeBytes: chunkSummaries.reduce((sum, chunk) => sum + chunk.runtimeBytes, 0),
      selectedLoweredRuntimeBytes: chunkSummaries.reduce(
        (sum, chunk) => sum + chunk.totals.selectedLoweredRuntimeBytes,
        0
      ),
      selectedSemanticStatementBytes: chunkSummaries.reduce(
        (sum, chunk) => sum + chunk.totals.selectedSemanticStatementBytes,
        0
      ),
    },
    chunks: chunkSummaries,
  };
}

function describeArtifactChunkPath(inputRoot, chunkId, file) {
  if (!inputRoot) {
    return `${chunkId}/${file}`;
  }
  return relativeWorkspacePath(join(inputRoot, ...chunkId.split("/"), file));
}

function compareClosurePlanReports(left, right) {
  return (
    left.semanticBlockingReasons.length - right.semanticBlockingReasons.length ||
    right.contiguousEnvelopeBytes - left.contiguousEnvelopeBytes ||
    right.semanticStatementBytes - left.semanticStatementBytes ||
    left.id.localeCompare(right.id)
  );
}

function compareBatchPlanReports(left, right) {
  return (
    left.blockingReasons.length - right.blockingReasons.length ||
    right.loweredRuntimeBytes - left.loweredRuntimeBytes ||
    right.semanticStatementBytes - left.semanticStatementBytes ||
    left.id.localeCompare(right.id)
  );
}

function selectChunks(chunks, selectedChunkIds) {
  if (!selectedChunkIds || selectedChunkIds.length === 0) {
    return chunks;
  }
  const selected = new Set(selectedChunkIds);
  const filtered = chunks.filter((chunk) => selected.has(chunk.chunkId));
  if (filtered.length !== selected.size) {
    const missing = [...selected].filter((chunkId) => !filtered.some((chunk) => chunk.chunkId === chunkId));
    throw new Error(`Owner closure plan report missing chunks: ${missing.sort().join(", ")}`);
  }
  return filtered;
}

function compactOwnerOrdinals(ownerIds, ownerOrdinalById) {
  const ordinals = [...new Set(ownerIds.map((ownerId) => ownerOrdinalById.get(ownerId)).filter((ordinal) => ordinal !== undefined))]
    .sort((left, right) => left - right);
  if (ordinals.length === 0) {
    return [];
  }
  const ranges = [];
  let start = ordinals[0];
  let end = ordinals[0];
  for (let index = 1; index < ordinals.length; index++) {
    const ordinal = ordinals[index];
    if (ordinal === end + 1) {
      end = ordinal;
      continue;
    }
    ranges.push({ start, end, count: end - start + 1 });
    start = ordinal;
    end = ordinal;
  }
  ranges.push({ start, end, count: end - start + 1 });
  return ranges;
}

function compactContiguousOrdinalRange(ownerIds, ownerOrdinalById) {
  const ranges = compactOwnerOrdinals(ownerIds, ownerOrdinalById);
  if (ranges.length === 0) {
    return null;
  }
  return {
    start: ranges[0].start,
    end: ranges.at(-1).end,
    count: ownerIds.length,
  };
}

function previewNames(names, limit = 8) {
  return names.slice(0, limit);
}
