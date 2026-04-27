import { join } from "node:path";
import { parse } from "@babel/parser";
import { DEFAULT_PARSER_OPTIONS } from "../common/parser_options.mjs";
import { writeJsonFile } from "../common/parser_options.mjs";
import {
  createChunk,
  createFile,
  getArtifactChunkManifest,
  getArtifactManifest,
  getChunk,
  requirePipelineArtifact,
  setArtifactChunkManifest,
  setArtifactManifest,
  setChunk,
} from "../common/artifact.mjs";
import { ensureOutputDir, logProgress, relativeWorkspacePath, resolveWorkspacePath } from "../common/io.mjs";
import { extractSelectedModulePlanInAst } from "./init_region.mjs";
import { deriveSelectedModuleTarget } from "./planner.mjs";

export function mergeModules({
  artifact,
  force = false,
  operations = [],
  reportOutDir = undefined,
  reportSummaryPath = undefined,
}) {
  requirePipelineArtifact(artifact, "mergeModules");
  const mergeOperations = normalizeMergeOperations(operations);
  const artifactManifest = getArtifactManifest(artifact);
  const resolvedReportOutDir = reportOutDir ? resolveWorkspacePath(reportOutDir) : null;
  const resolvedReportSummaryPath =
    resolvedReportOutDir && reportSummaryPath
      ? resolveWorkspacePath(reportSummaryPath)
      : resolvedReportOutDir
        ? resolveWorkspacePath(join(reportOutDir, "summary.json"))
        : null;

  if (resolvedReportOutDir) {
    ensureOutputDir(resolvedReportOutDir);
  }

  if (mergeOperations.length === 0) {
    const manifest = {
      kind: "js.merge_module_manifest",
      schemaVersion: 1,
      counts: {
        chunks: 0,
        mergeOperations: 0,
        mergedAway: 0,
        mergedModules: 0,
        modulesAfter: 0,
        modulesBefore: 0,
      },
      chunks: [],
      ...(resolvedReportOutDir ? { reportOutDir: relativeWorkspacePath(resolvedReportOutDir) } : {}),
    };
    if (resolvedReportSummaryPath) {
      writeJsonFile(resolvedReportSummaryPath, manifest);
    }
    return { artifact, manifest };
  }

  const operationsByChunk = new Map();
  for (const operation of mergeOperations) {
    if (!operationsByChunk.has(operation.selector.chunkId)) {
      operationsByChunk.set(operation.selector.chunkId, []);
    }
    operationsByChunk.get(operation.selector.chunkId).push(operation);
  }

  const reports = [];
  const applied = [];
  let mergedModuleCount = 0;
  let mergeOperationCount = 0;
  let mergedAwayCount = 0;
  let modulesAfterCount = 0;
  let modulesBeforeCount = 0;
  for (const [chunkId, chunkOperations] of operationsByChunk.entries()) {
    const chunk = getChunk(artifact, chunkId);
    if (!chunk) {
      throw new Error(`mergeModules missing chunk ${chunkId}`);
    }
    const state = chunk.metadata?.moduleExtractionState;
    if (!state?.originalCode || !Array.isArray(state.currentModules)) {
      throw new Error(`mergeModules requires extractAtomicModules state for chunk ${chunkId}`);
    }

    const currentModules = state.currentModules.map(cloneModulePlan);
    const modulesBefore = currentModules.length;
    const resolvedTargetDir = state.targetDir ?? "modules";
    const chunkStartedAt = process.hrtime.bigint();
    const planStartedAt = process.hrtime.bigint();
    const nextModules = buildMergedModulePlans(currentModules, chunkOperations, {
      targetDir: resolvedTargetDir,
    });
    const planMs = durationMsSince(planStartedAt);
    const runtimeFile = state.runtimeFile ?? chunk.entryFile;
    if (!runtimeFile) {
      throw new Error(`mergeModules missing runtimeFile for chunk ${chunkId}`);
    }
    const parserOptions = state.parserOptions ?? DEFAULT_PARSER_OPTIONS;
    const headerLines = state.headerLines ?? [];
    const parseStartedAt = process.hrtime.bigint();
    const loweringAst = parse(state.originalCode, parserOptions);
    const parseMs = durationMsSince(parseStartedAt);
    const lowerStartedAt = process.hrtime.bigint();
    const result = extractSelectedModulePlanInAst(
      loweringAst,
      {
        kind: "js.selected_module_plan",
        modulePlans: nextModules,
      },
      {
        ...(state.analysis ? { analysis: state.analysis } : {}),
        chunkId,
        file: runtimeFile,
        headerLines,
        idPrefix: "merged_module",
        targetDir: resolvedTargetDir,
      }
    );
    const lowerMs = durationMsSince(lowerStartedAt);
    const chunkOperationIds = new Set(chunkOperations.map((operation) => operation.id));
    const moduleByTargetFile = new Map(nextModules.map((modulePlan) => [modulePlan.targetFile, modulePlan]));
    const mergedModules = [];
    for (const modulePlan of nextModules) {
      if (!chunkOperationIds.has(modulePlan.id)) {
        continue;
      }
      mergedModules.push({
        bytes: modulePlan.bytes,
        file:
          modulePlan.targetFile ?? deriveSelectedModuleTarget(modulePlan, modulePlan.index, { targetDir: resolvedTargetDir }).file,
        id: modulePlan.id,
        lines: modulePlan.lines,
        ownerCount: modulePlan.ownerIds.length,
        unitCount: modulePlan.unitIds.length,
      });
    }
    const mergedModuleIds = mergedModules.map((modulePlan) => modulePlan.id);

    const writebackStartedAt = process.hrtime.bigint();
    const nextChunk = createChunk({
      chunkId,
      entryFile: runtimeFile,
      files: [...result.jsFiles.entries()].map(([relativePath, fileArtifact]) => {
        const modulePlan = moduleByTargetFile.get(relativePath) ?? null;
        return createFile({
          path: relativePath,
          ast: fileArtifact.ast,
          headerLines: fileArtifact.headerLines,
          metadata: {
            ...(chunk.files.get(runtimeFile)?.metadata ?? {}),
            chunkFile: relativePath,
            chunkId,
            role: relativePath === runtimeFile ? "entry" : "module",
            ...(modulePlan
              ? {
                  moduleExtraction: {
                    id: modulePlan.id,
                    kind: "module",
                    nameHint: modulePlan.nameHint,
                    ownerIds: [...modulePlan.ownerIds],
                    unitIds: [...modulePlan.unitIds],
                  },
                }
              : {}),
          },
          parserOptions,
        });
      }),
      metadata: {
        ...chunk.metadata,
        moduleExtractionState: {
          ...state,
          currentModules: nextModules.map(cloneModulePlan),
          mode: "merged",
        },
      },
    });
    setChunk(artifact, nextChunk);
    const writebackMs = durationMsSince(writebackStartedAt);

    const chunkManifest = getArtifactChunkManifest(artifact, chunkId);
    setArtifactChunkManifest(artifact, chunkId, {
      ...chunkManifest,
      entryFile: runtimeFile,
      mergeModules: {
        count: chunkOperations.length,
        mergedModuleIds,
      },
      orderedInitExtractions: result.applied,
    });
    applied.push(...result.applied);
    mergedModuleCount += mergedModuleIds.length;
    mergeOperationCount += chunkOperations.length;
    mergedAwayCount += modulesBefore - nextModules.length;
    modulesAfterCount += nextModules.length;
    modulesBeforeCount += modulesBefore;
    const report = {
      chunkId,
      counts: {
        mergeOperations: chunkOperations.length,
        mergedAway: modulesBefore - nextModules.length,
        modulesAfter: nextModules.length,
        modulesBefore,
      },
      mergedModuleIds,
      mergedModules,
      operationIds: chunkOperations.map((operation) => operation.id),
      timingsMs: {
        lower: lowerMs,
        parse: parseMs,
        plan: planMs,
        total: durationMsSince(chunkStartedAt),
        writeback: writebackMs,
      },
    };
    reports.push(report);
    if (resolvedReportOutDir) {
      writeJsonFile(join(resolvedReportOutDir, `${chunkId}.json`), report);
    }
    logProgress(
      `merge-modules chunk=${chunkId} operations=${chunkOperations.length} before=${modulesBefore} after=${nextModules.length} plan=${formatDuration(
        planMs
      )} parse=${formatDuration(parseMs)} lower=${formatDuration(lowerMs)} writeback=${formatDuration(writebackMs)} total=${formatDuration(
        report.timingsMs.total
      )}`
    );
  }

  setArtifactManifest(artifact, {
    ...artifactManifest,
    counts: {
      ...(artifactManifest?.counts ?? {}),
      orderedInitExtractions: applied.length,
    },
    mergeModules: {
      chunkCount: reports.length,
      mergedModuleCount,
    },
    orderedInitExtractions: applied,
  });

  const manifest = {
    kind: "js.merge_module_manifest",
    schemaVersion: 1,
    counts: {
      chunks: reports.length,
      mergeOperations: mergeOperationCount,
      mergedAway: mergedAwayCount,
      mergedModules: mergedModuleCount,
      modulesAfter: modulesAfterCount,
      modulesBefore: modulesBeforeCount,
    },
    chunks: reports,
    ...(resolvedReportOutDir ? { reportOutDir: relativeWorkspacePath(resolvedReportOutDir) } : {}),
    ...(force ? { force } : {}),
  };
  if (resolvedReportSummaryPath) {
    writeJsonFile(resolvedReportSummaryPath, manifest);
  }

  logProgress(`merge-modules done chunks=${reports.length}`);
  return {
    artifact,
    manifest,
  };
}

function normalizeMergeOperations(operations) {
  return operations
    .filter(
      (operation) => operation?.operation === "merge_module" || operation?.operation === "merge_remaining_modules"
    )
    .map((operation) => {
      if (typeof operation?.id !== "string" || operation.id === "") {
        throw new Error(`${operation?.operation ?? "merge operation"} requires id`);
      }
      if (typeof operation?.selector?.chunkId !== "string" || operation.selector.chunkId === "") {
        throw new Error(`${operation.operation} ${operation.id} requires selector.chunkId`);
      }
      if (operation.operation === "merge_module") {
        const hasModuleIds = Array.isArray(operation?.selector?.moduleIds) && operation.selector.moduleIds.length > 0;
        const hasModuleSelectors =
          Array.isArray(operation?.selector?.moduleSelectors) && operation.selector.moduleSelectors.length > 0;
        if (hasModuleIds === hasModuleSelectors) {
          throw new Error(`merge_module ${operation.id} requires exactly one of selector.moduleIds or selector.moduleSelectors`);
        }
        return {
          ...operation,
          selector: {
            chunkId: normalizeRelativeFile(operation.selector.chunkId),
            ...(hasModuleIds
              ? {
                  moduleIds: [...new Set(operation.selector.moduleIds)],
                }
              : {
                  moduleSelectors: operation.selector.moduleSelectors.map((moduleSelector, index) =>
                    normalizeModuleSelector(moduleSelector, operation.id, index)
                  ),
                  ...(operation.selector.validation
                    ? {
                        validation: normalizeModuleSelectorValidation(operation.selector.validation, operation.id),
                      }
                    : {}),
                }),
          },
          target: operation.target ?? {},
        };
      }
      return {
        ...operation,
        selector: {
          chunkId: normalizeRelativeFile(operation.selector.chunkId),
        },
        target: operation.target ?? {},
      };
    });
}

function buildMergedModulePlans(currentModules, operations, { targetDir }) {
  const moduleById = new Map(currentModules.map((modulePlan) => [modulePlan.id, modulePlan]));
  const operationByModuleId = new Map();
  let orderedModules = null;
  let orderedModuleMemberNameSets = null;
  const resolvedOperations = operations.map((operation) => {
    if (operation.operation !== "merge_module") {
      return operation;
    }
    if (!orderedModules) {
      orderedModules = [...currentModules].sort((left, right) => left.startOrdinal - right.startOrdinal || left.id.localeCompare(right.id));
      orderedModuleMemberNameSets = orderedModules.map((modulePlan) => new Set(modulePlan.memberNames));
    }
    return {
      ...operation,
      resolvedModuleIds: resolveMergeModuleIds(orderedModules, orderedModuleMemberNameSets, operation),
    };
  });
  let residualOperation = null;
  for (const operation of resolvedOperations) {
    if (operation.operation === "merge_remaining_modules") {
      if (residualOperation) {
        throw new Error(`merge_remaining_modules operations overlap on chunk ${operation.selector.chunkId}`);
      }
      residualOperation = operation;
      continue;
    }
    for (const moduleId of operation.resolvedModuleIds) {
      if (!moduleById.has(moduleId)) {
        throw new Error(`merge_module ${operation.id} references unknown module ${moduleId}`);
      }
      if (operationByModuleId.has(moduleId)) {
        throw new Error(`merge_module operations overlap on module ${moduleId}`);
      }
      operationByModuleId.set(moduleId, operation);
    }
  }

  const emittedOperations = new Set();
  let unclaimedModules = null;
  const nextModules = [];
  for (const currentModule of currentModules) {
    const operation = operationByModuleId.get(currentModule.id);
    if (!operation) {
      if (residualOperation) {
        if (emittedOperations.has(residualOperation.id)) {
          continue;
        }
        emittedOperations.add(residualOperation.id);
        if (!unclaimedModules) {
          unclaimedModules = [];
          for (const modulePlan of currentModules) {
            if (!operationByModuleId.has(modulePlan.id)) {
              unclaimedModules.push(modulePlan);
            }
          }
        }
        if (unclaimedModules.length > 0) {
          nextModules.push(mergeModuleGroup(unclaimedModules, residualOperation, nextModules.length, { targetDir }));
        }
        continue;
      }
      nextModules.push(cloneModulePlan(currentModule));
      continue;
    }
    if (emittedOperations.has(operation.id)) {
      continue;
    }
    emittedOperations.add(operation.id);
    const selectedModules = operation.resolvedModuleIds
      .map((moduleId) => moduleById.get(moduleId))
      .sort((left, right) => left.startOrdinal - right.startOrdinal || left.id.localeCompare(right.id));
    nextModules.push(mergeModuleGroup(selectedModules, operation, nextModules.length, { targetDir }));
  }
  return nextModules.sort((left, right) => left.startOrdinal - right.startOrdinal || left.id.localeCompare(right.id));
}

function resolveMergeModuleIds(orderedModules, orderedModuleMemberNameSets, operation) {
  if (Array.isArray(operation.selector.moduleIds) && operation.selector.moduleIds.length > 0) {
    return [...operation.selector.moduleIds];
  }
  return resolveModuleSelectors(orderedModules, orderedModuleMemberNameSets, operation);
}

function resolveModuleSelectors(orderedModules, orderedModuleMemberNameSets, operation) {
  const resolvedModules = operation.selector.moduleSelectors.map((moduleSelector, selectorIndex) =>
    resolveModuleSelector(orderedModules, orderedModuleMemberNameSets, moduleSelector, {
      operationId: operation.id,
      selectorIndex,
    })
  );
  const resolvedModuleIds = resolvedModules.map((modulePlan) => modulePlan.id);
  const uniqueResolvedModuleIds = new Set(resolvedModuleIds);
  if (uniqueResolvedModuleIds.size !== resolvedModuleIds.length) {
    throw new Error(`merge_module ${operation.id} matched the same module more than once`);
  }
  if (operation.selector.validation?.ordered) {
    const sortedResolvedModuleIds = [...resolvedModules]
      .sort((left, right) => left.startOrdinal - right.startOrdinal || left.id.localeCompare(right.id))
      .map((modulePlan) => modulePlan.id);
    if (!sameStringArray(resolvedModuleIds, sortedResolvedModuleIds)) {
      throw new Error(`merge_module ${operation.id} ordered moduleSelectors did not match ascending startOrdinal order`);
    }
  }
  return resolvedModuleIds;
}

function resolveModuleSelector(orderedModules, orderedModuleMemberNameSets, moduleSelector, { operationId, selectorIndex }) {
  const matches = [];
  for (let index = 0; index < orderedModules.length; index++) {
    const modulePlan = orderedModules[index];
    if (matchesModuleSelector(modulePlan, orderedModuleMemberNameSets[index], moduleSelector, orderedModuleMemberNameSets, index)) {
      matches.push(modulePlan);
    }
  }
  if (matches.length === 0) {
    throw new Error(`merge_module ${operationId} moduleSelector[${selectorIndex}] matched no modules`);
  }
  if (matches.length > 1) {
    throw new Error(
      `merge_module ${operationId} moduleSelector[${selectorIndex}] matched multiple modules: ${matches
        .map((modulePlan) => modulePlan.id)
        .join(", ")}`
    );
  }
  return matches[0];
}

function matchesModuleSelector(modulePlan, moduleMemberNamesSet, moduleSelector, orderedModuleMemberNameSets, moduleIndex) {
  // Selectors match against the full current `memberNames` set on each module
  // plan. Authors can provide an exact full set or a unique identifying subset;
  // ambiguity is rejected at resolution time rather than guessed away here.
  if (!containsAllStrings(moduleMemberNamesSet, moduleSelector.symbols)) {
    return false;
  }
  if (moduleSelector.ordinalWindow) {
    const { end, start } = moduleSelector.ordinalWindow;
    if (modulePlan.startOrdinal < start || modulePlan.startOrdinal > end) {
      return false;
    }
  }
  if (moduleSelector.nearbyStructure?.previousSymbols) {
    const previousModuleMemberNames = orderedModuleMemberNameSets[moduleIndex - 1];
    if (!previousModuleMemberNames || !containsAllStrings(previousModuleMemberNames, moduleSelector.nearbyStructure.previousSymbols)) {
      return false;
    }
  }
  if (moduleSelector.nearbyStructure?.nextSymbols) {
    const nextModuleMemberNames = orderedModuleMemberNameSets[moduleIndex + 1];
    if (!nextModuleMemberNames || !containsAllStrings(nextModuleMemberNames, moduleSelector.nearbyStructure.nextSymbols)) {
      return false;
    }
  }
  return true;
}

function normalizeModuleSelector(moduleSelector, operationId, index) {
  if (!moduleSelector || typeof moduleSelector !== "object") {
    throw new Error(`merge_module ${operationId} moduleSelector[${index}] must be an object`);
  }
  if (!Array.isArray(moduleSelector.symbols) || moduleSelector.symbols.length === 0) {
    throw new Error(`merge_module ${operationId} moduleSelector[${index}] requires symbols`);
  }
  const normalized = {
    symbols: normalizeSelectorNameList(moduleSelector.symbols, `merge_module ${operationId} moduleSelector[${index}].symbols`),
  };
  if (moduleSelector.nearbyStructure) {
    const nearbyStructure = {};
    if (moduleSelector.nearbyStructure.previousSymbols) {
      nearbyStructure.previousSymbols = normalizeSelectorNameList(
        moduleSelector.nearbyStructure.previousSymbols,
        `merge_module ${operationId} moduleSelector[${index}].nearbyStructure.previousSymbols`
      );
    }
    if (moduleSelector.nearbyStructure.nextSymbols) {
      nearbyStructure.nextSymbols = normalizeSelectorNameList(
        moduleSelector.nearbyStructure.nextSymbols,
        `merge_module ${operationId} moduleSelector[${index}].nearbyStructure.nextSymbols`
      );
    }
    if (Object.keys(nearbyStructure).length > 0) {
      normalized.nearbyStructure = nearbyStructure;
    }
  }
  if (moduleSelector.ordinalWindow) {
    const start = requireSafeInteger(
      moduleSelector.ordinalWindow.start,
      `merge_module ${operationId} moduleSelector[${index}].ordinalWindow.start`
    );
    const end = requireSafeInteger(
      moduleSelector.ordinalWindow.end,
      `merge_module ${operationId} moduleSelector[${index}].ordinalWindow.end`
    );
    if (end < start) {
      throw new Error(`merge_module ${operationId} moduleSelector[${index}] requires ordinalWindow.end >= ordinalWindow.start`);
    }
    normalized.ordinalWindow = { end, start };
  }
  return normalized;
}

function normalizeModuleSelectorValidation(validation, operationId) {
  if (!validation || typeof validation !== "object") {
    throw new Error(`merge_module ${operationId} selector.validation must be an object`);
  }
  const normalized = {};
  if ("ordered" in validation) {
    if (typeof validation.ordered !== "boolean") {
      throw new Error(`merge_module ${operationId} selector.validation.ordered must be boolean`);
    }
    normalized.ordered = validation.ordered;
  }
  return normalized;
}

function normalizeSelectorNameList(values, label) {
  if (!Array.isArray(values) || values.length === 0) {
    throw new Error(`${label} must be a non-empty array`);
  }
  return [...new Set(values.map((value, index) => normalizeSelectorName(value, `${label}[${index}]`)))].sort();
}

function normalizeSelectorName(value, label) {
  if (typeof value !== "string" || value === "") {
    throw new Error(`${label} must be a non-empty string`);
  }
  return value;
}

function requireSafeInteger(value, label) {
  if (!Number.isSafeInteger(value)) {
    throw new Error(`${label} must be a safe integer`);
  }
  return value;
}

function sameStringArray(left, right) {
  if (left.length !== right.length) {
    return false;
  }
  for (let index = 0; index < left.length; index++) {
    if (left[index] !== right[index]) {
      return false;
    }
  }
  return true;
}

function containsAllStrings(haystack, needles) {
  const haystackSet = haystack instanceof Set ? haystack : new Set(haystack);
  for (const needle of needles) {
    if (!haystackSet.has(needle)) {
      return false;
    }
  }
  return true;
}

function mergeModuleGroup(selectedModules, operation, index, { targetDir }) {
  const targetBasename =
    typeof operation.target?.basename === "string" && operation.target.basename !== ""
      ? sanitizeIdentifier(operation.target.basename)
      : sanitizeIdentifier(operation.id);
  const attachedItemIds = [];
  const attachedItemIdSet = new Set();
  const memberNames = [];
  const memberNameSet = new Set();
  const ownerIds = [];
  const ownerIdSet = new Set();
  const unitIds = [];
  const unitIdSet = new Set();
  let bytes = 0;
  let hasNullBytes = false;
  let lines = 0;
  let startOrdinal = Number.POSITIVE_INFINITY;
  for (const modulePlan of selectedModules) {
    lines += modulePlan.lines;
    if (modulePlan.bytes === null) {
      hasNullBytes = true;
    } else if (!hasNullBytes) {
      bytes += modulePlan.bytes;
    }
    if (modulePlan.startOrdinal < startOrdinal) {
      startOrdinal = modulePlan.startOrdinal;
    }
    for (const itemId of modulePlan.attachedItemIds) {
      if (!attachedItemIdSet.has(itemId)) {
        attachedItemIdSet.add(itemId);
        attachedItemIds.push(itemId);
      }
    }
    for (const memberName of modulePlan.memberNames) {
      if (!memberNameSet.has(memberName)) {
        memberNameSet.add(memberName);
        memberNames.push(memberName);
      }
    }
    for (const ownerId of modulePlan.ownerIds) {
      if (!ownerIdSet.has(ownerId)) {
        ownerIdSet.add(ownerId);
        ownerIds.push(ownerId);
      }
    }
    for (const unitId of modulePlan.unitIds) {
      if (!unitIdSet.has(unitId)) {
        unitIdSet.add(unitId);
        unitIds.push(unitId);
      }
    }
  }
  const baseModule = {
    attachedItemIds: attachedItemIds.sort(),
    basename: targetBasename,
    bytes: hasNullBytes ? null : bytes,
    id: operation.id,
    index,
    lines,
    memberNames: memberNames.sort(),
    nameHint: targetBasename,
    ownerIds: ownerIds,
    startOrdinal,
    unitIds,
  };
  const derivedTarget = deriveSelectedModuleTarget(baseModule, index, { targetDir });
  return {
    ...baseModule,
    initName:
      typeof operation.target?.init === "string" && operation.target.init !== ""
        ? operation.target.init
        : derivedTarget.init,
    targetFile:
      typeof operation.target?.file === "string" && operation.target.file !== ""
        ? normalizeRelativeFile(operation.target.file)
        : derivedTarget.file,
  };
}

function cloneModulePlan(modulePlan) {
  return {
    attachedItemIds: [...modulePlan.attachedItemIds],
    basename: modulePlan.basename,
    ...(modulePlan.bytes === null ? { bytes: null } : { bytes: modulePlan.bytes }),
    id: modulePlan.id,
    index: modulePlan.index,
    ...(modulePlan.initName ? { initName: modulePlan.initName } : {}),
    lines: modulePlan.lines,
    memberNames: [...modulePlan.memberNames],
    nameHint: modulePlan.nameHint,
    ownerIds: [...modulePlan.ownerIds],
    startOrdinal: modulePlan.startOrdinal,
    ...(modulePlan.targetFile ? { targetFile: modulePlan.targetFile } : {}),
    unitIds: [...modulePlan.unitIds],
  };
}

function normalizeRelativeFile(value) {
  if (typeof value !== "string" || value === "") {
    throw new Error(`Expected a non-empty relative path, got: ${value}`);
  }
  const normalized = value.replace(/^\.\/+/, "").replace(/\\/g, "/");
  if (normalized === "." || normalized.startsWith("/") || normalized.split("/").includes("..")) {
    throw new Error(`Invalid relative path: ${value}`);
  }
  return normalized;
}

function sanitizeIdentifier(value) {
  return value
    .replace(/[^A-Za-z0-9_$]+/g, "_")
    .replace(/^[^A-Za-z_$]+/, "_")
    .replace(/_+/g, "_");
}

function durationMsSince(startedAt) {
  return Number(process.hrtime.bigint() - startedAt) / 1_000_000;
}

function formatDuration(durationMs) {
  return `${durationMs.toFixed(3)}ms`;
}
