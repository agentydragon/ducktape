import { parse } from "@babel/parser";
import * as t from "@babel/types";
import { analyzeRuntimeBoundaryAst } from "../analysis/boundary.mjs";
import { DEFAULT_PARSER_OPTIONS } from "../common/parser_options.mjs";
import { referencedUndeclaredNames } from "../common/program_analysis.mjs";
import { serializeGeneratedJsFile } from "../split/chunk.mjs";
import {
  expandOrderedInitOwnerClosurePassOperations,
  ORDERED_INIT_OWNER_CLOSURE_PASS_OPERATION,
} from "./decl_graph.mjs";
import {
  buildGuidedSelectedOwnerModuleOperations,
  buildSelectedModuleOperations,
} from "./planner.mjs";


export function extractOrderedInitRegionsInCode(code, operations, options = {}) {
  const ast = parse(code, options.parser ?? DEFAULT_PARSER_OPTIONS);
  const file = resolveOperationFile(operations, options.file, "extractOrderedInitRegionsInCode");
  const result = extractOrderedInitRegionsInAst(ast, operations, { ...options, file });
  const files = new Map();
  for (const [relativePath, file] of result.jsFiles.entries()) {
    files.set(relativePath, serializeGeneratedJsFile(file));
  }
  return {
    applied: result.applied,
    files,
    code: files.get(file),
  };
}

export function extractOrderedInitRegionsInAst(
  ast,
  operations,
  { analysis = null, chunkId = "<chunk>", file, headerLines = [] } = {}
) {
  const runtimeFile = resolveOperationFile(operations, file, "extractOrderedInitRegionsInAst");
  const graphAwareOperations = operations.filter((operation) => EXTRACT_OPERATION_TYPES.has(operation.operation));
  const suppliedAnalysis = analysis ?? null;
  const resolvedChunkId =
    chunkId === "<chunk>"
      ? suppliedAnalysis?.chunkId ?? inferredChunkId(graphAwareOperations) ?? chunkId
      : chunkId;
  const runtimeAnalysis = suppliedAnalysis ?? analyzeRuntimeBoundaryAst(ast, { chunkId: resolvedChunkId });
  const ownerById = new Map(runtimeAnalysis.owners.map((owner) => [owner.id, owner]));
  const programBody = ast.program.body;
  const sideEffectById = new Map(runtimeAnalysis.sideEffects.map((sideEffect) => [sideEffect.id, sideEffect]));
  const extractOperations = expandOrderedInitOwnerClosurePassOperations(
    runtimeAnalysis,
    graphAwareOperations,
    {
      chunkId: resolvedChunkId,
      file: runtimeFile,
    }
  )
    .filter((operation) => operation.operation === "extract_ordered_init_region")
    .filter((operation) => operationSupportsCurrentExtractor(operation, { ownerById }));
  const topLevelNames = collectTopLevelNames(runtimeAnalysis);
  const programItemByOrdinal = new Map(runtimeAnalysis.programItems.map((item) => [item.ordinal, item]));
  const extractionIndex = buildExtractionIndex(extractOperations);
  const remainingProgramValidationIndex = buildRemainingProgramValidationIndex(runtimeAnalysis);
  const runtimeImportIndex = buildRuntimeImportIndex(runtimeAnalysis.runtimeImports);

  const resolved = extractOperations.map((operation) =>
    resolveExtractOperation(operation, {
      allSelectedOwnerIds: extractionIndex.allSelectedOwnerIds,
      analysis: runtimeAnalysis,
      chunkId: resolvedChunkId,
      extractedOwnerToOperation: extractionIndex.ownerToOperation,
      ownerById,
      remainingProgramValidationIndex,
      runtimeImportIndex,
      programBody,
      programItemByOrdinal,
      runtimeFile,
      sideEffectById,
      topLevelNames,
    })
  );
  validateResolvedOperations(resolved);
  const resolvedByOwnerId = indexResolvedEntriesByOwnerId(resolved);
  for (const entry of resolved) {
    finalizeResolvedEntryImports(entry, resolvedByOwnerId);
  }

  const runtimeBody = ast.program.body;
  const moduleFiles = new Map();
  const replacementRuns = [];
  for (const entry of resolved) {
    for (const run of buildRuntimeReplacementRuns(entry)) {
      replacementRuns.push(run);
    }
  }
  replacementRuns.sort(
    (left, right) =>
      right.startOrdinal - left.startOrdinal ||
      right.stageIndex - left.stageIndex ||
      right.entry.id.localeCompare(left.entry.id)
  );
  for (const run of replacementRuns) {
    runtimeBody.splice(run.startOrdinal, run.endOrdinal - run.startOrdinal + 1, buildInitCallStatement(run));
  }

  if (resolved.length > 0) {
    const importInsertIndex = countLeadingImports(runtimeBody);
    runtimeBody.splice(importInsertIndex, 0, ...resolved.map(buildRuntimeImportDeclaration));
  }

  const jsFiles = new Map([[runtimeFile, { ast, headerLines }]]);
  const applied = [];
  for (const entry of resolved) {
    const moduleFile = buildExtractedModuleFile(entry);
    moduleFiles.set(entry.targetFile, moduleFile);
    jsFiles.set(entry.targetFile, moduleFile);
    applied.push({
      chunkId,
      exportedNames: [...entry.exportedNames],
      file: runtimeFile,
      id: entry.id,
      init: entry.initName,
      operation: entry.operation,
      ownerIds: [...entry.ownerIds],
      targetFile: entry.targetFile,
    });
  }

  return {
    analysis: runtimeAnalysis,
    applied,
    jsFiles,
    modules: moduleFiles,
  };
}

export function extractGuidedSelectedOwnerModulesInAst(
  ast,
  plan,
  { analysis, chunkId = "<chunk>", file, filePrefix, headerLines = [], idPrefix, initPrefix, targetDir } = {}
) {
  return extractSelectedModulePlanInAst(ast, plan, {
    analysis,
    chunkId,
    ...(file ? { file } : {}),
    ...(filePrefix ? { filePrefix } : {}),
    headerLines,
    ...(idPrefix ? { idPrefix } : {}),
    ...(initPrefix ? { initPrefix } : {}),
    ...(targetDir ? { targetDir } : {}),
    operationBuilder: buildGuidedSelectedOwnerModuleOperations,
  });
}

export function extractSelectedModulePlanInAst(
  ast,
  plan,
  {
    analysis,
    chunkId = "<chunk>",
    file,
    filePrefix,
    headerLines = [],
    idPrefix,
    initPrefix,
    targetDir,
    operationBuilder = buildSelectedModuleOperations,
  } = {}
) {
  if (!plan?.modulePlans) {
    throw new Error("extractSelectedModulePlanInAst requires a module plan");
  }
  const operations = operationBuilder(plan, {
    chunkId,
    ...(file ? { file } : {}),
    ...(filePrefix ? { filePrefix } : {}),
    ...(idPrefix ? { idPrefix } : {}),
    ...(initPrefix ? { initPrefix } : {}),
    ...(targetDir ? { targetDir } : {}),
  });
  const result = extractOrderedInitRegionsInAst(ast, operations, {
    ...(analysis ? { analysis } : {}),
    chunkId,
    ...(file ? { file } : {}),
    headerLines,
  });
  return {
    ...result,
    ...(analysis ? { analysis } : {}),
  };
}

function resolveExtractOperation(
  operation,
  {
    allSelectedOwnerIds,
    analysis,
    chunkId,
    extractedOwnerToOperation,
    ownerById,
    remainingProgramValidationIndex,
    runtimeImportIndex,
    programBody,
    programItemByOrdinal,
    runtimeFile,
    sideEffectById,
    topLevelNames,
  }
) {
  validateExtractOperationShape(operation);
  if (operation.selector.file && normalizeRelativeFile(operation.selector.file) !== runtimeFile) {
    throw new Error(
      `Extract operation ${operation.id} targets ${operation.selector.file}, expected ${runtimeFile}`
    );
  }
  if (operation.selector.chunkId !== chunkId) {
    throw new Error(`Extract operation ${operation.id} targets ${operation.selector.chunkId}, expected ${chunkId}`);
  }

  const selectedOwners = operation.selector.ownerIds.map((ownerId) => {
    const owner = ownerById.get(ownerId);
    if (!owner) {
      throw new Error(`Extract operation ${operation.id} references unknown owner ${ownerId}`);
    }
    return owner;
  });
  const selectedOwnerIds = new Set(selectedOwners.map((owner) => owner.id));
  const selectedFunctionIds = new Set(
    selectedOwners.filter((owner) => owner.type === "FunctionDeclaration").map((owner) => owner.id)
  );
  const orderedOwners = selectedOwners.sort((left, right) => left.ordinal - right.ordinal);
  const attachedSideEffects = (operation.selector.attachedItemIds ?? []).map((itemId) => {
    const sideEffect = sideEffectById.get(itemId);
    if (!sideEffect) {
      throw new Error(`Extract operation ${operation.id} references unknown attached item ${itemId}`);
    }
    return sideEffect;
  });
  const startOrdinal = orderedOwners[0].ordinal;
  const endOrdinal = orderedOwners.at(-1).ordinal;
  const lowering = operation.lowering ?? "staged_shell";

  if (lowering !== "staged_shell") {
    throw new Error(`Extract operation ${operation.id} uses unsupported lowering ${lowering}`);
  }

  if (orderedOwners.some((owner) => !programBody[owner.ordinal])) {
    throw new Error(`Extract operation ${operation.id} could not resolve all selected owners to statements`);
  }

  const targetFile = normalizeRelativeFile(operation.target.file);
  const initName = operation.target.init;
  if (targetFile === runtimeFile) {
    throw new Error(`Extract operation ${operation.id} target.file must differ from ${runtimeFile}`);
  }
  if (!t.isValidIdentifier(initName)) {
    throw new Error(`Extract operation ${operation.id} has invalid target.init ${initName}`);
  }

  const ownerEntries = orderedOwners.map((owner) => ({
    kind: "declaration",
    owner,
    statement: programBody[owner.ordinal],
  }));
  const attachedEntries = attachedSideEffects
    .map((sideEffect) => ({
      kind: "side_effect",
      sideEffect,
      statement: programBody[sideEffect.ordinal],
    }))
    .sort((left, right) => left.sideEffect.ordinal - right.sideEffect.ordinal);
  const exportedNames = collectOwnerExportNames(programBody, orderedOwners);
  if (exportedNames.includes(initName)) {
    throw new Error(`Extract operation ${operation.id} target.init ${initName} conflicts with an extracted binding`);
  }
  if (topLevelNames.has(initName)) {
    throw new Error(`Extract operation ${operation.id} target.init ${initName} already exists at top level`);
  }

  const usedRuntimeImportLocals = new Set();
  const usedExtractedDependencyNames = new Map();
  for (const owner of orderedOwners) {
    validateSelectedOwner(owner, {
      extractedOwnerToOperation,
      operation,
      ownerById,
      selectedOwnerIds,
      selectedFunctionIds,
      statementNode: programBody[owner.ordinal],
      usedExtractedDependencyNames,
      usedRuntimeImportLocals,
    });
  }
  for (const sideEffect of attachedSideEffects) {
    validateAttachedSideEffect(sideEffect, {
      extractedOwnerToOperation,
      operation,
      ownerById,
      selectedOwnerIds,
      usedExtractedDependencyNames,
      usedRuntimeImportLocals,
    });
  }
  validateRemainingProgramItems({
    operation,
    orderedOwners,
    remainingProgramValidationIndex,
    selectedItemIds: new Set([...selectedOwnerIds, ...attachedSideEffects.map((sideEffect) => sideEffect.id)]),
    selectedOwnerIds,
    allSelectedOwnerIds,
  });

  const stageRuns = buildStagedShellRuns(operation, {
    attachedEntries,
    ownerEntries,
    ownerById,
    programItemByOrdinal,
    selectedOwnerIds,
    sideEffects: analysis.sideEffects,
  });
  for (let stageIndex = 0; stageIndex < stageRuns.length; stageIndex++) {
    const stageName = stageInitName(initName, stageIndex);
    if (!t.isValidIdentifier(stageName)) {
      throw new Error(`Extract operation ${operation.id} has invalid staged init ${stageName}`);
    }
    if (topLevelNames.has(stageName)) {
      throw new Error(`Extract operation ${operation.id} staged init ${stageName} already exists at top level`);
    }
  }
  if (stageRuns.length === 0) {
    throw new Error(`Extract operation ${operation.id} produced no staged-shell runs`);
  }

  return {
    endOrdinal,
    exportedNames,
    id: operation.id,
    initName,
    lowering,
    operation: operation.operation,
    attachedEntries,
    ownerEntries,
    orderedOwners,
    ownerIds: orderedOwners.map((owner) => owner.id),
    stageRuns,
    startOrdinal,
    targetFile,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    usedRuntimeImports: materializeUsedRuntimeImports(runtimeImportIndex, usedRuntimeImportLocals),
  };
}

function validateSelectedOwner(
  owner,
  {
    extractedOwnerToOperation,
    operation,
    ownerById,
    selectedOwnerIds,
    selectedFunctionIds,
    statementNode,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
  }
) {
  if (!SUPPORTED_OWNER_TYPES.has(owner.type)) {
    throw new Error(`Extract operation ${operation.id} does not support ${owner.type} owners yet`);
  }
  if (owner.effects.containsDirectEval || owner.effects.containsImportMeta || owner.effects.containsTopLevelAwait) {
    throw new Error(`Extract operation ${operation.id} cannot extract runtime-sensitive owner ${owner.id}`);
  }
  if (owner.currentExtractorCompatible === false) {
    throw new Error(
      `Extract operation ${operation.id} cannot extract owner ${owner.id}: ${owner.currentExtractorBlockingReasons.join(",")}`
    );
  }
  if (owner.type === "VariableDeclaration" && owner.currentExtractorLowering !== "snapshot_variable_declaration") {
    validateVariableDeclarators(statementNode, operation.id, owner.id);
  }

  validateSelectedOwnerAccesses(owner.readsTopLevel.eager, "eager read", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: owner.id,
    ownerById,
    ownerOrdinal: owner.ordinal,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: false,
  });
  validateSelectedOwnerAccesses(owner.readsTopLevel.lazy, "lazy read", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: owner.id,
    ownerById,
    ownerOrdinal: owner.ordinal,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: false,
  });
  validateSelectedOwnerAccesses(owner.memberWritesTopLevel.eager, "eager member write", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: owner.id,
    ownerById,
    ownerOrdinal: owner.ordinal,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: true,
  });
  validateSelectedOwnerAccesses(owner.memberWritesTopLevel.lazy, "lazy member write", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: owner.id,
    ownerById,
    ownerOrdinal: owner.ordinal,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: true,
  });
  validateSelectedOwnerAccesses(owner.writesTopLevel.eager, "eager write", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: owner.id,
    ownerById,
    ownerOrdinal: owner.ordinal,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: true,
  });
  validateSelectedOwnerAccesses(owner.writesTopLevel.lazy, "lazy write", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: owner.id,
    ownerById,
    ownerOrdinal: owner.ordinal,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: true,
  });

  for (const access of owner.readsTopLevel.eager) {
    if (access.kind !== "local_declaration" || !selectedOwnerIds.has(access.ownerId) || access.ownerId === owner.id) {
      continue;
    }
    const targetOwner = ownerById.get(access.ownerId);
    if (selectedFunctionIds.has(targetOwner.id) || targetOwner.ordinal < owner.ordinal) {
      continue;
    }
    throw new Error(
      `Extract operation ${operation.id} owner ${owner.id} has unsupported forward eager dependency on ${targetOwner.id}`
    );
  }
  for (const access of [...owner.writesTopLevel.eager, ...owner.memberWritesTopLevel.eager]) {
    if (access.kind !== "local_declaration" || !selectedOwnerIds.has(access.ownerId) || access.ownerId === owner.id) {
      continue;
    }
    const targetOwner = ownerById.get(access.ownerId);
    if (selectedFunctionIds.has(targetOwner.id) || targetOwner.ordinal < owner.ordinal) {
      continue;
    }
    throw new Error(
      `Extract operation ${operation.id} owner ${owner.id} has unsupported forward eager mutation of ${targetOwner.id}`
    );
  }
}

function validateAttachedSideEffect(
  sideEffect,
  {
    extractedOwnerToOperation,
    operation,
    ownerById,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
  }
) {
  if (
    sideEffect.effects.containsDirectEval ||
    sideEffect.effects.containsImportMeta ||
    sideEffect.effects.containsTopLevelAwait
  ) {
    throw new Error(`Extract operation ${operation.id} cannot attach runtime-sensitive side effect ${sideEffect.id}`);
  }

  validateSelectedOwnerAccesses(sideEffect.readsTopLevel.eager, "eager read", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: sideEffect.id,
    ownerById,
    ownerOrdinal: sideEffect.ordinal,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: false,
  });
  validateSelectedOwnerAccesses(sideEffect.readsTopLevel.lazy, "lazy read", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: sideEffect.id,
    ownerById,
    ownerOrdinal: sideEffect.ordinal,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: false,
  });
  validateSelectedOwnerAccesses(sideEffect.memberWritesTopLevel.eager, "eager member write", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: sideEffect.id,
    ownerById,
    ownerOrdinal: sideEffect.ordinal,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: true,
  });
  validateSelectedOwnerAccesses(sideEffect.memberWritesTopLevel.lazy, "lazy member write", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: sideEffect.id,
    ownerById,
    ownerOrdinal: sideEffect.ordinal,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: true,
  });
  validateSelectedOwnerAccesses(sideEffect.writesTopLevel.eager, "eager write", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: sideEffect.id,
    ownerById,
    ownerOrdinal: sideEffect.ordinal,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: true,
  });
  validateSelectedOwnerAccesses(sideEffect.writesTopLevel.lazy, "lazy write", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: sideEffect.id,
    ownerById,
    ownerOrdinal: sideEffect.ordinal,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: true,
  });
}

function validateSelectedOwnerAccesses(
  accesses,
  accessLabel,
  {
    extractedOwnerToOperation,
    operationId,
    ownerId,
    ownerById,
    ownerOrdinal,
    selectedOwnerIds,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite,
  }
) {
  for (const access of accesses) {
    if (access.kind === "runtime_import") {
      if (accessLabel.includes("write") && !accessLabel.includes("member")) {
        throw new Error(
          `Extract operation ${operationId} owner ${ownerId} has unsupported ${accessLabel} to runtime import ${access.name}`
        );
      }
      usedRuntimeImportLocals.add(access.name);
      continue;
    }
    if (access.kind !== "local_declaration") {
      continue;
    }
    if (selectedOwnerIds.has(access.ownerId)) {
      if (!allowSelectedLocalWrite && accessLabel.includes("write")) {
        throw new Error(
          `Extract operation ${operationId} owner ${ownerId} has unsupported ${accessLabel} to extracted owner ${access.ownerId}`
        );
      }
      continue;
    }
    if (extractedOwnerToOperation.has(access.ownerId)) {
      if (accessLabel.includes("write")) {
        throw new Error(
          `Extract operation ${operationId} owner ${ownerId} has unsupported ${accessLabel} to separately extracted owner ${access.ownerId}`
        );
      }
      const targetOwner = ownerById.get(access.ownerId);
      if (accessLabel === "eager read" && targetOwner && targetOwner.ordinal > ownerOrdinal) {
        throw new Error(
          `Extract operation ${operationId} owner ${ownerId} has unsupported forward eager dependency on separately extracted owner ${access.ownerId}`
        );
      }
      recordExtractedDependencyName(usedExtractedDependencyNames, access.ownerId, access.name);
      continue;
    }
    if (!selectedOwnerIds.has(access.ownerId)) {
      throw new Error(
        `Extract operation ${operationId} owner ${ownerId} depends on local runtime owner ${access.ownerId} via ${accessLabel}`
      );
    }
  }
}

function validateRemainingProgramItems({
  operation,
  orderedOwners,
  remainingProgramValidationIndex,
  selectedItemIds,
  selectedOwnerIds,
  allSelectedOwnerIds,
}) {
  const startOrdinal = orderedOwners[0].ordinal;
  for (const selectedOwnerId of selectedOwnerIds) {
    for (const record of remainingProgramValidationIndex.writersByOwnerId.get(selectedOwnerId) ?? []) {
      if (selectedItemIds.has(record.recordId) || allSelectedOwnerIds.has(record.recordId)) {
        continue;
      }
      throw new Error(
        `Extract operation ${operation.id} would leave ${record.recordId} writing extracted binding ${record.name}`
      );
    }
    for (const record of remainingProgramValidationIndex.earlierEagerUsersByOwnerId.get(selectedOwnerId) ?? []) {
      if (record.ordinal >= startOrdinal) {
        continue;
      }
      if (selectedItemIds.has(record.recordId) || allSelectedOwnerIds.has(record.recordId)) {
        continue;
      }
      throw new Error(
        `Extract operation ${operation.id} would move binding ${record.name} after eager use in ${record.recordId}`
      );
    }
  }
}

function buildStagedShellRuns(
  operation,
  { attachedEntries, ownerEntries, ownerById, programItemByOrdinal, selectedOwnerIds, sideEffects }
) {
  const stageItems = [
    ...ownerEntries.map((entry) => ({
      kind: "declaration",
      ordinal: entry.owner.ordinal,
      ownerEntries: [entry],
      statementEntries: [entry],
    })),
    ...attachedEntries.map((entry) => ({
      kind: "side_effect",
      ordinal: entry.sideEffect.ordinal,
      ownerEntries: [],
      statementEntries: [entry],
    })),
  ].sort((left, right) => left.ordinal - right.ordinal);
  const stageRuns = [];
  for (const stageItem of stageItems) {
    const currentStage = stageRuns.at(-1);
    if (!currentStage || currentStage.endOrdinal + 1 !== stageItem.ordinal) {
      stageRuns.push({
        endOrdinal: stageItem.ordinal,
        ownerEntries: [...stageItem.ownerEntries],
        stageEntries: [...stageItem.statementEntries],
        startOrdinal: stageItem.ordinal,
      });
      continue;
    }
    currentStage.endOrdinal = stageItem.ordinal;
    currentStage.ownerEntries.push(...stageItem.ownerEntries);
    currentStage.stageEntries.push(...stageItem.statementEntries);
  }

  if (stageRuns.length <= 1) {
    return stageRuns;
  }

  const sideEffectById = new Map(sideEffects.map((sideEffect) => [sideEffect.id, sideEffect]));
  const firstRetainedOrdinal = stageRuns[0].endOrdinal + 1;
  const lastOrdinal = stageRuns.at(-1).endOrdinal;
  for (let ordinal = firstRetainedOrdinal; ordinal < lastOrdinal; ordinal++) {
    const item = programItemByOrdinal.get(ordinal);
    if (!item || selectedOwnerIds.has(item.id)) {
      continue;
    }
    const record = ownerById.get(item.id) ?? sideEffectById.get(item.id);
    if (!record) {
      continue;
    }
    const blockedOwnerId = findLaterSelectedOwnerAccess(record, selectedOwnerIds, ownerById);
    if (blockedOwnerId) {
      throw new Error(
        `Extract operation ${operation.id} staged shell item ${item.id} eagerly uses later extracted owner ${blockedOwnerId}`
      );
    }
  }

  return stageRuns;
}

function findLaterSelectedOwnerAccess(record, selectedOwnerIds, ownerById) {
  return (
    findLaterSelectedOwnerAccessInList(record.readsTopLevel.eager, record.ordinal, selectedOwnerIds, ownerById) ??
    findLaterSelectedOwnerAccessInList(
      record.memberWritesTopLevel.eager,
      record.ordinal,
      selectedOwnerIds,
      ownerById
    )
  );
}

function findLaterSelectedOwnerAccessInList(accesses, ordinal, selectedOwnerIds, ownerById) {
  for (const access of accesses) {
    if (access.kind !== "local_declaration" || !access.ownerId || !selectedOwnerIds.has(access.ownerId)) {
      continue;
    }
    const targetOwner = ownerById.get(access.ownerId);
    if (targetOwner && targetOwner.ordinal > ordinal) {
      return access.ownerId;
    }
  }
  return null;
}

function validateResolvedOperations(resolved) {
  const targetFiles = new Set();
  const initNames = new Set();
  const ownerIds = new Set();
  const attachedItemIds = new Set();
  for (const entry of resolved) {
    if (targetFiles.has(entry.targetFile)) {
      throw new Error(`Duplicate extraction target file ${entry.targetFile}`);
    }
    targetFiles.add(entry.targetFile);
    if (initNames.has(entry.initName)) {
      throw new Error(`Duplicate extraction init function ${entry.initName}`);
    }
    initNames.add(entry.initName);
    for (const ownerId of entry.ownerIds) {
      if (ownerIds.has(ownerId)) {
        throw new Error(`Overlapping extraction regions include owner ${ownerId}`);
      }
      ownerIds.add(ownerId);
    }
    for (const attachedItemId of entry.attachedEntries?.map((item) => item.sideEffect.id) ?? []) {
      if (attachedItemIds.has(attachedItemId)) {
        throw new Error(`Overlapping extraction regions include attached item ${attachedItemId}`);
      }
      attachedItemIds.add(attachedItemId);
    }
  }
}

function buildRuntimeImportDeclaration(entry) {
  const specifiers = [
    ...entry.exportedNames.map((name) => importSpecifierForLocal(name, name, "named")),
    ...runtimeInitNamesForEntry(entry).map((name) => importSpecifierForLocal(name, name, "named")),
  ];
  return t.importDeclaration(specifiers, t.stringLiteral(runtimeImportSourceForTarget(entry.targetFile)));
}

function buildRuntimeReplacementRuns(entry) {
  return entry.stageRuns.map((stageRun, stageIndex) => ({
    endOrdinal: stageRun.endOrdinal,
    entry,
    stageIndex,
    startOrdinal: stageRun.startOrdinal,
  }));
}

function buildInitCallStatement(run) {
  return t.expressionStatement(t.callExpression(t.identifier(runtimeInitNameForRun(run)), []));
}

function buildExtractedModuleFile(entry) {
  const body = [];
  for (const importRecord of entry.usedExtractedImports ?? []) {
    body.push(importDeclarationFromExtractedImportRecord(importRecord, entry.targetFile));
  }
  for (const importRecord of entry.usedRuntimeImports) {
    body.push(importDeclarationFromRuntimeImportRecord(importRecord, entry.targetFile));
  }
  body.push(
    t.exportNamedDeclaration(
      t.variableDeclaration(
        "let",
        entry.exportedNames.map((name) => t.variableDeclarator(t.identifier(name)))
      )
    )
  );
  for (const stage of moduleStagesForEntry(entry)) {
    body.push(
      t.exportNamedDeclaration(
        t.functionDeclaration(
          t.identifier(stage.initName),
          [],
          t.blockStatement(buildInitStatements(stage.stageEntries, entry.targetFile))
        )
      )
    );
  }
  body.push(
    t.exportNamedDeclaration(
      t.functionDeclaration(
        t.identifier(entry.initName),
        [],
        t.blockStatement(
          entry.stageRuns.map((stageRun, stageIndex) =>
            t.expressionStatement(
              t.callExpression(t.identifier(stageInitName(entry.initName, stageIndex)), [])
            )
          )
        )
      )
    )
  );
  return {
    ast: t.file(t.program(body)),
    headerLines: [
      "// Generated by devinfra/js/debundle/extract/init_region.mjs.",
      `// Ordered-init extracted region; original owners: ${entry.ownerIds.join(", ")}.`,
    ],
  };
}

function buildInitStatements(stageEntries, targetFile) {
  const statements = [];
  for (const entry of stageEntries) {
    if (entry.kind !== "declaration") {
      continue;
    }
    const { owner, statement } = entry;
    if (owner.type !== "FunctionDeclaration") {
      continue;
    }
    statements.push(functionDeclarationAssignmentStatement(statement));
  }
  for (const entry of stageEntries) {
    if (entry.kind === "side_effect") {
      statements.push(t.cloneNode(entry.statement, true));
      continue;
    }
    const { owner, statement } = entry;
    if (owner.type === "FunctionDeclaration") {
      continue;
    }
    statements.push(...buildOwnerInitStatements(owner, statement));
  }
  return rewriteStatementsForTarget(statements, targetFile);
}

function buildOwnerInitStatements(owner, statement) {
  if (owner.currentExtractorLowering === "snapshot_variable_declaration") {
    return buildSnapshotVariableDeclarationStatements(owner, statement);
  }
  if (t.isClassDeclaration(statement)) {
    return [classDeclarationAssignmentStatement(statement)];
  }
  if (t.isVariableDeclaration(statement)) {
    return statement.declarations.map((declaration) => variableDeclaratorAssignmentStatement(declaration));
  }
  throw new Error(`Unsupported extracted owner statement type ${statement?.type}`);
}

function buildSnapshotVariableDeclarationStatements(owner, statement) {
  const declaration = unwrapTopLevelDeclarationNode(statement);
  if (!t.isVariableDeclaration(declaration)) {
    throw new Error(`Expected VariableDeclaration for snapshot lowering of ${owner.id}, got ${statement?.type}`);
  }
  const bindingNames = topLevelDeclarationNames(statement);
  const snapshotId = t.identifier(`__snapshot_${owner.id.replace(/[^A-Za-z0-9_$]/g, "_")}`);
  const declarationStatement = t.cloneNode(declaration, true);
  const snapshotObject = t.objectExpression(
    bindingNames.map((name) => t.objectProperty(t.identifier(name), t.identifier(name), false, true))
  );
  const snapshotInit = t.callExpression(
    t.arrowFunctionExpression([], t.blockStatement([declarationStatement, t.returnStatement(snapshotObject)])),
    []
  );
  return [
    t.variableDeclaration("const", [t.variableDeclarator(snapshotId, snapshotInit)]),
    ...bindingNames.map((name) =>
      t.expressionStatement(
        t.assignmentExpression("=", t.identifier(name), t.memberExpression(snapshotId, t.identifier(name)))
      )
    ),
  ];
}

function runtimeInitNamesForEntry(entry) {
  return entry.stageRuns.map((stageRun, stageIndex) => stageInitName(entry.initName, stageIndex));
}

function runtimeInitNameForRun(run) {
  return stageInitName(run.entry.initName, run.stageIndex);
}

function moduleStagesForEntry(entry) {
  return entry.stageRuns.map((stageRun, stageIndex) => ({
    initName: stageInitName(entry.initName, stageIndex),
    stageEntries: stageRun.stageEntries,
  }));
}

function stageInitName(initName, stageIndex) {
  return `${initName}_stage_${stageIndex}`;
}

function functionDeclarationAssignmentStatement(statement) {
  if (!t.isFunctionDeclaration(statement) || !statement.id) {
    throw new Error(`Expected FunctionDeclaration, got ${statement?.type}`);
  }
  return t.expressionStatement(
    t.assignmentExpression(
      "=",
      t.identifier(statement.id.name),
      t.functionExpression(
        t.identifier(statement.id.name),
        cloneNodes(statement.params),
        t.cloneNode(statement.body, true),
        statement.generator,
        statement.async
      )
    )
  );
}

function classDeclarationAssignmentStatement(statement) {
  if (!t.isClassDeclaration(statement) || !statement.id) {
    throw new Error(`Expected ClassDeclaration, got ${statement?.type}`);
  }
  return t.expressionStatement(
    t.assignmentExpression(
      "=",
      t.identifier(statement.id.name),
      t.classExpression(
        t.identifier(statement.id.name),
        statement.superClass ? t.cloneNode(statement.superClass, true) : null,
        t.cloneNode(statement.body, true),
        cloneNodes(statement.decorators ?? [])
      )
    )
  );
}

function variableDeclaratorAssignmentStatement(declaration) {
  if (!t.isIdentifier(declaration.id)) {
    throw new Error(`Unsupported extracted variable declarator ${declaration.id?.type}`);
  }
  return t.expressionStatement(
    t.assignmentExpression(
      "=",
      t.identifier(declaration.id.name),
      declaration.init ? t.cloneNode(declaration.init, true) : t.identifier("undefined")
    )
  );
}

function rewriteStatementsForTarget(statements, targetFile) {
  if (posixDirname(targetFile) === ".") {
    return statements;
  }
  const rewriteCache = new Map();
  const rewriteImportSource = (source) => {
    if (rewriteCache.has(source)) {
      return rewriteCache.get(source);
    }
    const rewritten = rebaseRuntimeSourceForTarget(source, targetFile);
    rewriteCache.set(source, rewritten);
    return rewritten;
  };
  for (const statement of statements) {
    rewriteRuntimeSourcesInNode(statement, rewriteImportSource, RUNTIME_CONSTRUCTOR_SHADOW_NONE);
  }
  return statements;
}

function rewriteRuntimeSourcesInNode(node, rewriteImportSource, shadowedRuntimeConstructors) {
  if (!node) {
    return;
  }
  if (isDynamicImportWithStringLiteralSource(node)) {
    rewriteDynamicImportSource(dynamicImportSourceNode(node), rewriteImportSource);
  }
  if (t.isNewExpression(node)) {
    rewriteRuntimeConstructorSource(node, rewriteImportSource, shadowedRuntimeConstructors);
  }
  if (isFunctionLikeNode(node)) {
    rewriteFunctionLikeRuntimeSources(node, rewriteImportSource, shadowedRuntimeConstructors);
    return;
  }
  if (t.isStaticBlock(node)) {
    rewriteBlockRuntimeSources(node.body, rewriteImportSource, shadowedRuntimeConstructors);
    return;
  }
  if (t.isBlockStatement(node)) {
    rewriteBlockRuntimeSources(node.body, rewriteImportSource, shadowedRuntimeConstructors);
    return;
  }
  if (t.isSwitchStatement(node)) {
    rewriteSwitchRuntimeSources(node, rewriteImportSource, shadowedRuntimeConstructors);
    return;
  }
  if (t.isCatchClause(node)) {
    rewriteCatchClauseRuntimeSources(node, rewriteImportSource, shadowedRuntimeConstructors);
    return;
  }
  if (isLoopNodeWithLexicalScope(node)) {
    rewriteLoopRuntimeSources(node, rewriteImportSource, shadowedRuntimeConstructors);
    return;
  }
  if (t.isClassDeclaration(node) || t.isClassExpression(node)) {
    rewriteClassRuntimeSources(node, rewriteImportSource, shadowedRuntimeConstructors);
    return;
  }
  visitChildNodes(node, (child) =>
    rewriteRuntimeSourcesInNode(child, rewriteImportSource, shadowedRuntimeConstructors)
  );
}

function visitChildNodes(node, visitor) {
  const keys = t.VISITOR_KEYS[node.type];
  if (!keys) {
    return;
  }
  for (const key of keys) {
    const value = node[key];
    if (Array.isArray(value)) {
      for (const child of value) {
        if (child) {
          visitor(child);
        }
      }
      continue;
    }
    if (value) {
      visitor(value);
    }
  }
}

function isDynamicImportWithStringLiteralSource(node) {
  return t.isStringLiteral(dynamicImportSourceNode(node));
}

function dynamicImportSourceNode(node) {
  if (t.isCallExpression(node) && node.callee.type === "Import") {
    return node.arguments[0];
  }
  if (t.isImportExpression(node)) {
    return node.source;
  }
  return null;
}

function rewriteRuntimeConstructorSource(node, rewriteImportSource, shadowedRuntimeConstructors) {
  if (!t.isIdentifier(node.callee)) {
    return;
  }
  const shadowBit = runtimeConstructorShadowBit(node.callee.name);
  if (shadowBit === RUNTIME_CONSTRUCTOR_SHADOW_NONE || (shadowedRuntimeConstructors & shadowBit) !== 0) {
    return;
  }
  const [scriptArgument] = node.arguments;
  if (!t.isStringLiteral(scriptArgument)) {
    return;
  }
  const rewrittenSource = rewriteImportSource(scriptArgument.value);
  if (rewrittenSource === scriptArgument.value) {
    return;
  }
  node.arguments[0] = t.newExpression(t.identifier("URL"), [
    t.stringLiteral(rewrittenSource),
    t.memberExpression(t.metaProperty(t.identifier("import"), t.identifier("meta")), t.identifier("url")),
  ]);
}

function isFunctionLikeNode(node) {
  return (
    t.isFunctionDeclaration(node) ||
    t.isFunctionExpression(node) ||
    t.isArrowFunctionExpression(node) ||
    t.isObjectMethod(node) ||
    t.isClassMethod(node) ||
    t.isClassPrivateMethod(node)
  );
}

function rewriteFunctionLikeRuntimeSources(node, rewriteImportSource, shadowedRuntimeConstructors) {
  if ("computed" in node && node.computed && node.key) {
    rewriteRuntimeSourcesInNode(node.key, rewriteImportSource, shadowedRuntimeConstructors);
  }
  for (const decorator of node.decorators ?? []) {
    rewriteRuntimeSourcesInNode(decorator, rewriteImportSource, shadowedRuntimeConstructors);
  }

  const parameterShadowedRuntimeConstructors =
    shadowedRuntimeConstructors |
    runtimeConstructorBindingShadowMask(node.id) |
    runtimeConstructorBindingShadowMaskForNodes(node.params);
  for (const param of node.params) {
    rewriteRuntimeSourcesInNode(param, rewriteImportSource, parameterShadowedRuntimeConstructors);
  }

  const bodyShadowedRuntimeConstructors =
    parameterShadowedRuntimeConstructors | collectFunctionVarShadowMask(node.body);
  rewriteRuntimeSourcesInNode(node.body, rewriteImportSource, bodyShadowedRuntimeConstructors);
}

function rewriteBlockRuntimeSources(statements, rewriteImportSource, shadowedRuntimeConstructors) {
  const blockShadowedRuntimeConstructors =
    shadowedRuntimeConstructors | collectBlockScopedShadowMask(statements);
  for (const statement of statements) {
    rewriteRuntimeSourcesInNode(statement, rewriteImportSource, blockShadowedRuntimeConstructors);
  }
}

function rewriteSwitchRuntimeSources(node, rewriteImportSource, shadowedRuntimeConstructors) {
  rewriteRuntimeSourcesInNode(node.discriminant, rewriteImportSource, shadowedRuntimeConstructors);
  const switchShadowedRuntimeConstructors =
    shadowedRuntimeConstructors | collectSwitchScopedShadowMask(node.cases);
  for (const switchCase of node.cases) {
    rewriteRuntimeSourcesInNode(switchCase.test, rewriteImportSource, switchShadowedRuntimeConstructors);
    for (const statement of switchCase.consequent) {
      rewriteRuntimeSourcesInNode(statement, rewriteImportSource, switchShadowedRuntimeConstructors);
    }
  }
}

function rewriteCatchClauseRuntimeSources(node, rewriteImportSource, shadowedRuntimeConstructors) {
  const catchShadowedRuntimeConstructors =
    shadowedRuntimeConstructors | runtimeConstructorBindingShadowMask(node.param);
  rewriteRuntimeSourcesInNode(node.body, rewriteImportSource, catchShadowedRuntimeConstructors);
}

function isLoopNodeWithLexicalScope(node) {
  return t.isForStatement(node) || t.isForInStatement(node) || t.isForOfStatement(node);
}

function rewriteLoopRuntimeSources(node, rewriteImportSource, shadowedRuntimeConstructors) {
  const loopShadowedRuntimeConstructors =
    shadowedRuntimeConstructors | collectLoopScopedShadowMask(node);
  if (t.isForStatement(node)) {
    rewriteRuntimeSourcesInNode(node.init, rewriteImportSource, loopShadowedRuntimeConstructors);
    rewriteRuntimeSourcesInNode(node.test, rewriteImportSource, loopShadowedRuntimeConstructors);
    rewriteRuntimeSourcesInNode(node.update, rewriteImportSource, loopShadowedRuntimeConstructors);
    rewriteRuntimeSourcesInNode(node.body, rewriteImportSource, loopShadowedRuntimeConstructors);
    return;
  }
  rewriteRuntimeSourcesInNode(node.left, rewriteImportSource, loopShadowedRuntimeConstructors);
  rewriteRuntimeSourcesInNode(node.right, rewriteImportSource, loopShadowedRuntimeConstructors);
  rewriteRuntimeSourcesInNode(node.body, rewriteImportSource, loopShadowedRuntimeConstructors);
}

function rewriteClassRuntimeSources(node, rewriteImportSource, shadowedRuntimeConstructors) {
  for (const decorator of node.decorators ?? []) {
    rewriteRuntimeSourcesInNode(decorator, rewriteImportSource, shadowedRuntimeConstructors);
  }
  rewriteRuntimeSourcesInNode(node.superClass, rewriteImportSource, shadowedRuntimeConstructors);
  const classShadowedRuntimeConstructors =
    shadowedRuntimeConstructors | runtimeConstructorBindingShadowMask(node.id);
  rewriteRuntimeSourcesInNode(node.body, rewriteImportSource, classShadowedRuntimeConstructors);
}

function collectFunctionVarShadowMask(node) {
  let shadowMask = RUNTIME_CONSTRUCTOR_SHADOW_NONE;
  collectFunctionVarShadowMaskInNode(node, (nextShadowMask) => {
    shadowMask |= nextShadowMask;
  });
  return shadowMask;
}

function collectFunctionVarShadowMaskInNode(node, recordShadowMask) {
  if (!node || isFunctionLikeNode(node) || t.isStaticBlock(node)) {
    return;
  }
  if (t.isVariableDeclaration(node) && node.kind === "var") {
    recordShadowMask(runtimeConstructorBindingShadowMaskForNodes(node.declarations.map((declaration) => declaration.id)));
  }
  visitChildNodes(node, (child) => collectFunctionVarShadowMaskInNode(child, recordShadowMask));
}

function collectBlockScopedShadowMask(statements) {
  let shadowMask = RUNTIME_CONSTRUCTOR_SHADOW_NONE;
  for (const statement of statements) {
    if (t.isVariableDeclaration(statement) && statement.kind !== "var") {
      shadowMask |= runtimeConstructorBindingShadowMaskForNodes(statement.declarations.map((declaration) => declaration.id));
      continue;
    }
    if (t.isFunctionDeclaration(statement) || t.isClassDeclaration(statement)) {
      shadowMask |= runtimeConstructorBindingShadowMask(statement.id);
    }
  }
  return shadowMask;
}

function collectSwitchScopedShadowMask(cases) {
  let shadowMask = RUNTIME_CONSTRUCTOR_SHADOW_NONE;
  for (const switchCase of cases) {
    shadowMask |= collectBlockScopedShadowMask(switchCase.consequent);
  }
  return shadowMask;
}

function collectLoopScopedShadowMask(node) {
  if (t.isForStatement(node) && t.isVariableDeclaration(node.init) && node.init.kind !== "var") {
    return runtimeConstructorBindingShadowMaskForNodes(node.init.declarations.map((declaration) => declaration.id));
  }
  if ((t.isForInStatement(node) || t.isForOfStatement(node)) && t.isVariableDeclaration(node.left) && node.left.kind !== "var") {
    return runtimeConstructorBindingShadowMaskForNodes(node.left.declarations.map((declaration) => declaration.id));
  }
  return RUNTIME_CONSTRUCTOR_SHADOW_NONE;
}

function runtimeConstructorBindingShadowMaskForNodes(nodes) {
  let shadowMask = RUNTIME_CONSTRUCTOR_SHADOW_NONE;
  for (const node of nodes) {
    shadowMask |= runtimeConstructorBindingShadowMask(node);
  }
  return shadowMask;
}

function runtimeConstructorBindingShadowMask(node) {
  if (!node) {
    return RUNTIME_CONSTRUCTOR_SHADOW_NONE;
  }
  if (t.isIdentifier(node)) {
    return runtimeConstructorShadowBit(node.name);
  }
  if (t.isRestElement(node)) {
    return runtimeConstructorBindingShadowMask(node.argument);
  }
  if (t.isAssignmentPattern(node)) {
    return runtimeConstructorBindingShadowMask(node.left);
  }
  if (t.isArrayPattern(node)) {
    return runtimeConstructorBindingShadowMaskForNodes(node.elements);
  }
  if (t.isObjectPattern(node)) {
    let shadowMask = RUNTIME_CONSTRUCTOR_SHADOW_NONE;
    for (const property of node.properties) {
      if (t.isRestElement(property)) {
        shadowMask |= runtimeConstructorBindingShadowMask(property.argument);
        continue;
      }
      shadowMask |= runtimeConstructorBindingShadowMask(property.value);
    }
    return shadowMask;
  }
  return RUNTIME_CONSTRUCTOR_SHADOW_NONE;
}

function runtimeConstructorShadowBit(name) {
  if (name === "Worker") {
    return RUNTIME_CONSTRUCTOR_SHADOW_WORKER;
  }
  if (name === "SharedWorker") {
    return RUNTIME_CONSTRUCTOR_SHADOW_SHARED_WORKER;
  }
  return RUNTIME_CONSTRUCTOR_SHADOW_NONE;
}

function rewriteDynamicImportSource(argument, rewriteImportSource) {
  if (t.isStringLiteral(argument)) {
    argument.value = rewriteImportSource(argument.value);
  }
}

function buildRuntimeImportIndex(runtimeImports) {
  const imports = runtimeImports.map((importRecord) => ({
    source: importRecord.source,
    specifiers: importRecord.specifiers,
  }));
  const refsByLocal = new Map();
  for (let importIndex = 0; importIndex < imports.length; importIndex++) {
    const importRecord = imports[importIndex];
    for (let specifierIndex = 0; specifierIndex < importRecord.specifiers.length; specifierIndex++) {
      const specifier = importRecord.specifiers[specifierIndex];
      if (!refsByLocal.has(specifier.local)) {
        refsByLocal.set(specifier.local, []);
      }
      refsByLocal.get(specifier.local).push({ importIndex, specifierIndex });
    }
  }
  return {
    imports,
    refsByLocal,
  };
}

function materializeUsedRuntimeImports(runtimeImportIndex, usedRuntimeImportLocals) {
  const specifiersByImportIndex = new Map();
  for (const local of usedRuntimeImportLocals) {
    for (const ref of runtimeImportIndex.refsByLocal.get(local) ?? []) {
      if (!specifiersByImportIndex.has(ref.importIndex)) {
        specifiersByImportIndex.set(ref.importIndex, new Map());
      }
      specifiersByImportIndex
        .get(ref.importIndex)
        .set(ref.specifierIndex, runtimeImportIndex.imports[ref.importIndex].specifiers[ref.specifierIndex]);
    }
  }
  return [...specifiersByImportIndex.entries()]
    .sort(([leftIndex], [rightIndex]) => leftIndex - rightIndex)
    .map(([importIndex, specifiersByIndex]) => ({
      source: runtimeImportIndex.imports[importIndex].source,
      specifiers: [...specifiersByIndex.entries()]
        .sort(([leftIndex], [rightIndex]) => leftIndex - rightIndex)
        .map(([, specifier]) => specifier),
    }));
}

function importDeclarationFromRuntimeImportRecord(importRecord, targetFile) {
  return t.importDeclaration(
    importRecord.specifiers.map((specifier) =>
      importSpecifierForLocal(specifier.local, specifier.imported ?? specifier.local, specifier.kind)
    ),
    t.stringLiteral(rebaseRuntimeSourceForTarget(importRecord.source, targetFile))
  );
}

function importDeclarationFromExtractedImportRecord(importRecord, targetFile) {
  return t.importDeclaration(
    importRecord.specifiers.map((specifier) => importSpecifierForLocal(specifier.local, specifier.imported, "named")),
    t.stringLiteral(rebaseTargetSourceForTarget(importRecord.sourceTargetFile, targetFile))
  );
}

function importSpecifierForLocal(local, imported, kind) {
  if (kind === "default") {
    return t.importDefaultSpecifier(t.identifier(local));
  }
  if (kind === "namespace") {
    return t.importNamespaceSpecifier(t.identifier(local));
  }
  return t.importSpecifier(t.identifier(local), exportNameNode(imported));
}

function exportNameNode(name) {
  return t.isValidIdentifier(name) ? t.identifier(name) : t.stringLiteral(name);
}

function runtimeImportSourceForTarget(targetFile) {
  return ensureRelativeImportSource(targetFile);
}

function rebaseTargetSourceForTarget(sourceTargetFile, targetFile) {
  const fromDir = posixDirname(targetFile);
  const rebased = fromDir === "." ? sourceTargetFile : relativeBetween(fromDir, sourceTargetFile);
  return ensureRelativeImportSource(rebased);
}

function rebaseRuntimeSourceForTarget(source, targetFile) {
  if (!source.startsWith(".")) {
    return source;
  }
  const fromDir = posixDirname(targetFile);
  const normalizedSource = normalizeRelativeImportSource(source);
  const rebased = fromDir === "."
    ? normalizedSource
    : relativeBetween(fromDir, normalizedSource);
  return ensureRelativeImportSource(rebased);
}

function normalizeRelativeImportSource(source) {
  return source.split("\\").join("/").replace(/^\.\//, "");
}

function posixDirname(path) {
  const normalized = path.split("\\").join("/");
  const index = normalized.lastIndexOf("/");
  return index === -1 ? "." : normalized.slice(0, index);
}

function relativeBetween(fromDir, toPath) {
  const fromSegments = fromDir === "." ? [] : fromDir.split("/").filter(Boolean);
  const toSegments = toPath.split("/").filter(Boolean);
  while (fromSegments.length > 0 && toSegments.length > 0 && fromSegments[0] === toSegments[0]) {
    fromSegments.shift();
    toSegments.shift();
  }
  return `${"../".repeat(fromSegments.length)}${toSegments.join("/")}`;
}

function ensureRelativeImportSource(path) {
  if (path.startsWith(".")) {
    return path;
  }
  return `./${path}`;
}

function collectOwnerExportNames(programBody, owners) {
  const names = [];
  for (const owner of owners) {
    const statement = programBody[owner.ordinal];
    for (const name of topLevelDeclarationNames(statement)) {
      if (!names.includes(name)) {
        names.push(name);
      }
    }
  }
  return names;
}

function validateVariableDeclarators(statement, operationId, ownerId) {
  if (!t.isVariableDeclaration(statement)) {
    throw new Error(`Expected VariableDeclaration for ${ownerId}, got ${statement?.type}`);
  }
  const declaredNames = statement.declarations.map((declaration) => {
    if (!t.isIdentifier(declaration.id)) {
      throw new Error(`Extract operation ${operationId} does not support destructuring in ${ownerId}`);
    }
    return declaration.id.name;
  });
  const availableNames = new Set();
  for (const declaration of statement.declarations) {
    const declarationName = declaration.id.name;
    for (const referencedName of referencedUndeclaredNames(declaration.init)) {
      if (!declaredNames.includes(referencedName)) {
        continue;
      }
      if (!availableNames.has(referencedName)) {
        throw new Error(
          `Extract operation ${operationId} does not support forward/self variable references in ${ownerId}`
        );
      }
    }
    availableNames.add(declarationName);
  }
}


function cloneNodes(nodes) {
  return nodes.map((node) => t.cloneNode(node, true));
}

function topLevelDeclarationNames(node) {
  if (t.isFunctionDeclaration(node) || t.isClassDeclaration(node)) {
    return node.id ? [node.id.name] : [];
  }
  if (t.isVariableDeclaration(node)) {
    return node.declarations.flatMap((declaration) => bindingNames(declaration.id));
  }
  if (t.isExportNamedDeclaration(node) && node.declaration) {
    return topLevelDeclarationNames(node.declaration);
  }
  return [];
}

function unwrapTopLevelDeclarationNode(node) {
  if (t.isExportNamedDeclaration(node) && node.declaration) {
    return node.declaration;
  }
  return node;
}

function bindingNames(node) {
  if (!node) {
    return [];
  }
  if (t.isIdentifier(node)) {
    return [node.name];
  }
  if (t.isRestElement(node)) {
    return bindingNames(node.argument);
  }
  if (t.isAssignmentPattern(node)) {
    return bindingNames(node.left);
  }
  if (t.isArrayPattern(node)) {
    return node.elements.flatMap((element) => bindingNames(element));
  }
  if (t.isObjectPattern(node)) {
    return node.properties.flatMap((property) => {
      if (t.isRestElement(property)) {
        return bindingNames(property.argument);
      }
      return bindingNames(property.value);
    });
  }
  return [];
}

function collectTopLevelNames(analysis) {
  const names = new Set();
  for (const importRecord of analysis.runtimeImports) {
    for (const specifier of importRecord.specifiers) {
      names.add(specifier.local);
    }
  }
  for (const owner of analysis.owners) {
    for (const name of owner.names) {
      names.add(name);
    }
  }
  return names;
}

function countLeadingImports(programBody) {
  let index = 0;
  while (index < programBody.length && t.isImportDeclaration(programBody[index])) {
    index++;
  }
  return index;
}

function validateExtractOperationShape(operation) {
  if (!operation?.id) {
    throw new Error("Extract operation is missing id");
  }
  if (!operation.selector?.chunkId) {
    throw new Error(`Extract operation ${operation.id} is missing selector.chunkId`);
  }
  if (!Array.isArray(operation.selector.ownerIds) || operation.selector.ownerIds.length === 0) {
    throw new Error(`Extract operation ${operation.id} is missing selector.ownerIds`);
  }
  if (
    operation.selector.attachedItemIds !== undefined &&
    !Array.isArray(operation.selector.attachedItemIds)
  ) {
    throw new Error(`Extract operation ${operation.id} selector.attachedItemIds must be an array when present`);
  }
  if (!operation.target?.file) {
    throw new Error(`Extract operation ${operation.id} is missing target.file`);
  }
  if (!operation.target?.init) {
    throw new Error(`Extract operation ${operation.id} is missing target.init`);
  }
}

function inferredChunkId(operations) {
  if (operations.length === 0) {
    return null;
  }
  const chunkIds = [...new Set(operations.map((operation) => operation.selector?.chunkId).filter(Boolean))];
  if (chunkIds.length === 1) {
    return chunkIds[0];
  }
  return null;
}

function resolveOperationFile(operations, explicitFile, stageName) {
  if (explicitFile) {
    return normalizeRelativeFile(explicitFile);
  }
  const selectorFiles = [
    ...new Set(
      operations
        .map((operation) => operation.selector?.file)
        .filter((file) => typeof file === "string" && file !== "")
        .map((file) => normalizeRelativeFile(file))
    ),
  ];
  if (selectorFiles.length === 1) {
    return selectorFiles[0];
  }
  if (selectorFiles.length > 1) {
    throw new Error(`${stageName} received operations targeting multiple files: ${selectorFiles.join(", ")}`);
  }
  throw new Error(`${stageName} requires an explicit file or selector.file on operations`);
}

function normalizeRelativeFile(value) {
  if (typeof value !== "string" || value === "") {
    throw new Error(`Expected a non-empty relative path, got: ${value}`);
  }
  const normalized = value.split("\\").join("/").replace(/\/+/g, "/");
  if (normalized === "." || normalized.startsWith("/") || normalized.split("/").includes("..")) {
    throw new Error(`Invalid relative path: ${value}`);
  }
  return normalized;
}

function buildExtractionIndex(operations) {
  const allSelectedOwnerIds = new Set();
  const ownerToOperation = new Map();
  for (const operation of operations) {
    if (operation.operation !== "extract_ordered_init_region") {
      continue;
    }
    for (const ownerId of operation.selector.ownerIds ?? []) {
      allSelectedOwnerIds.add(ownerId);
      ownerToOperation.set(ownerId, {
        id: operation.id,
        targetFile: normalizeRelativeFile(operation.target.file),
      });
    }
  }
  return {
    allSelectedOwnerIds,
    ownerToOperation,
  };
}

function buildRemainingProgramValidationIndex(analysis) {
  const earlierEagerUsersByOwnerId = new Map();
  const writersByOwnerId = new Map();
  for (const record of [...analysis.owners, ...analysis.sideEffects]) {
    indexRemainingProgramAccesses(
      writersByOwnerId,
      record,
      [...record.writesTopLevel.eager, ...record.writesTopLevel.lazy]
    );
    indexRemainingProgramAccesses(
      earlierEagerUsersByOwnerId,
      record,
      [...record.readsTopLevel.eager, ...record.memberWritesTopLevel.eager]
    );
  }
  return {
    earlierEagerUsersByOwnerId,
    writersByOwnerId,
  };
}

function indexRemainingProgramAccesses(index, record, accesses) {
  for (const access of accesses) {
    if (access.kind !== "local_declaration" || !access.ownerId) {
      continue;
    }
    if (!index.has(access.ownerId)) {
      index.set(access.ownerId, []);
    }
    index.get(access.ownerId).push({
      name: access.name,
      ordinal: record.ordinal,
      recordId: record.id,
    });
  }
}

function operationSupportsCurrentExtractor(operation, { ownerById }) {
  if (!operation.graphGenerated) {
    return true;
  }
  for (const ownerId of operation.selector.ownerIds ?? []) {
    const owner = ownerById.get(ownerId);
    if (!owner) {
      return false;
    }
    if (owner.currentExtractorCompatible === false) {
      return false;
    }
  }
  return true;
}

function recordExtractedDependencyName(index, ownerId, name) {
  if (!index.has(ownerId)) {
    index.set(ownerId, new Set());
  }
  index.get(ownerId).add(name);
}

function indexResolvedEntriesByOwnerId(resolved) {
  const index = new Map();
  for (const entry of resolved) {
    for (const ownerId of entry.ownerIds) {
      index.set(ownerId, entry);
    }
  }
  return index;
}

function finalizeResolvedEntryImports(entry, resolvedByOwnerId) {
  const importsByTargetFile = new Map();
  for (const [ownerId, names] of entry.usedExtractedDependencyNames ?? []) {
    const providerEntry = resolvedByOwnerId.get(ownerId);
    if (!providerEntry || providerEntry.id === entry.id) {
      continue;
    }
    if (!importsByTargetFile.has(providerEntry.targetFile)) {
      importsByTargetFile.set(providerEntry.targetFile, new Set());
    }
    for (const name of names) {
      importsByTargetFile.get(providerEntry.targetFile).add(name);
    }
  }
  entry.usedExtractedImports = [...importsByTargetFile.entries()]
    .map(([sourceTargetFile, names]) => ({
      sourceTargetFile,
      specifiers: [...names].sort().map((name) => ({
        local: name,
        imported: name,
      })),
    }))
    .sort((left, right) => left.sourceTargetFile.localeCompare(right.sourceTargetFile));
}

const SUPPORTED_OWNER_TYPES = new Set(["FunctionDeclaration", "ClassDeclaration", "VariableDeclaration"]);
const EXTRACT_OPERATION_TYPES = new Set([
  "extract_ordered_init_region",
  ORDERED_INIT_OWNER_CLOSURE_PASS_OPERATION,
]);
const RUNTIME_CONSTRUCTOR_SHADOW_NONE = 0;
const RUNTIME_CONSTRUCTOR_SHADOW_WORKER = 1;
const RUNTIME_CONSTRUCTOR_SHADOW_SHARED_WORKER = 2;
