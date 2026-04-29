import { parse } from "@babel/parser";
import traverseModule from "@babel/traverse";
import * as t from "@babel/types";
import { analyzeRuntimeBoundaryAst, analyzeVariableDeclarationFragmentAccesses } from "../analysis/boundary.mjs";
import { isScrambledIdentifier } from "../analysis/identifier_frequency.mjs";
import { DEFAULT_PARSER_OPTIONS } from "../common/parser_options.mjs";
import { logProgress } from "../common/io.mjs";
import {
  referencedUndeclaredNames,
  referencedUndeclaredNamesInVariableDeclarator,
} from "../common/program_analysis.mjs";
import { serializeGeneratedJsFile } from "../split/chunk.mjs";
import { expandSelectedModuleGroupPlanningOperations, PLAN_SELECTED_MODULE_GROUPS_OPERATION } from "./decl_graph.mjs";
import { buildSelectedModuleOperations } from "./planner.mjs";

const traverse = traverseModule.default ?? traverseModule;

const SELECTED_MODULE_LOWERING_FILE_PRAGMA =
  "// @ducktape-generated kind=lowerer-helper stage=selected_module_lowering ignore=detectors";
const SELECTED_MODULE_LOWERING_GENERATOR_HEADER = "// @ducktape-generator devinfra/js/debundle/extract/init_region.mjs";
const SELECTED_MODULE_LOWERING_NODE_PRAGMA =
  "@ducktape-generated-node kind=lowerer-glue stage=selected_module_lowering";
const SELECTED_MODULE_ATOMIC_BOUNDARY_PRAGMA = "@ducktape-atomic-boundary kind=selected_module_lowering";
const SELECTED_MODULE_SNAPSHOT_PREFIX = "__dt_selected_module_snapshot__";
const SELECTED_MODULE_LOWERING_METADATA = Object.freeze({
  kind: "lowerer_helper",
  stage: "selected_module_lowering",
  generator: "devinfra/js/debundle/extract/init_region.mjs",
  ignoreByDefault: true,
});

function durationMsSince(startedAt) {
  return Number(process.hrtime.bigint() - startedAt) / 1_000_000;
}

function formatDurationMs(durationMs) {
  return `${durationMs.toFixed(3)}ms`;
}

function addDurationMs(bucket, key, durationMs) {
  bucket[key] = (bucket[key] ?? 0) + durationMs;
}

export function buildSelectedModuleLoweringMetadata() {
  return { ...SELECTED_MODULE_LOWERING_METADATA };
}

function buildSelectedModuleLoweringHeaderLines(ownerIds) {
  return [
    SELECTED_MODULE_LOWERING_FILE_PRAGMA,
    SELECTED_MODULE_LOWERING_GENERATOR_HEADER,
    `// Selected-module lowered region; original owners: ${ownerIds.join(", ")}.`,
  ];
}

function addSelectedModuleLoweringNodeComment(node) {
  t.addComment(node, "leading", ` ${SELECTED_MODULE_LOWERING_NODE_PRAGMA} `);
  return node;
}

function selectedModuleSnapshotIdentifierName(ownerId) {
  return `${SELECTED_MODULE_SNAPSHOT_PREFIX}${ownerId.replace(/[^A-Za-z0-9_$]/g, "_")}`;
}

export function lowerSelectedModuleRegionsInCode(code, operations, options = {}) {
  const ast = parse(code, options.parser ?? DEFAULT_PARSER_OPTIONS);
  const file = resolveOperationFile(operations, options.file, "lowerSelectedModuleRegionsInCode");
  const result = lowerSelectedModuleRegionsInAst(ast, operations, { ...options, file });
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

export function lowerSelectedModuleRegionsInAst(
  ast,
  operations,
  { analysis = null, chunkId = "<chunk>", file, headerLines = [] } = {}
) {
  const loweringStartedAt = process.hrtime.bigint();
  const runtimeFile = resolveOperationFile(operations, file, "lowerSelectedModuleRegionsInAst");
  const graphAwareOperations = operations.filter((operation) => EXTRACT_OPERATION_TYPES.has(operation.operation));
  const suppliedAnalysis = analysis ?? null;
  const resolvedChunkId =
    chunkId === "<chunk>" ? (suppliedAnalysis?.chunkId ?? inferredChunkId(graphAwareOperations) ?? chunkId) : chunkId;
  const runtimeAnalysis = suppliedAnalysis ?? analyzeRuntimeBoundaryAst(ast, { chunkId: resolvedChunkId });
  const ownerById = new Map(runtimeAnalysis.owners.map((owner) => [owner.id, owner]));
  const programBody = ast.program.body;
  const sideEffectById = new Map(runtimeAnalysis.sideEffects.map((sideEffect) => [sideEffect.id, sideEffect]));
  const extractOperations = expandSelectedModuleGroupPlanningOperations(runtimeAnalysis, graphAwareOperations, {
    chunkId: resolvedChunkId,
    file: runtimeFile,
  })
    .filter((operation) => operation.operation === "lower_selected_module_region")
    .filter((operation) => operationSupportsCurrentExtractor(operation, { ownerById }));
  const topLevelNames = collectTopLevelNames(runtimeAnalysis);
  const extractionIndex = buildExtractionIndex(extractOperations);
  const remainingProgramValidationIndex = buildRemainingProgramValidationIndex(runtimeAnalysis);
  const runtimeImportIndex = buildRuntimeImportIndex(runtimeAnalysis.runtimeImports);

  const resolveStartedAt = process.hrtime.bigint();
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
      runtimeFile,
      sideEffectById,
      topLevelNames,
    })
  );
  logProgress(
    `selected-modules lower=${runtimeFile} phase=resolve_extract_operations operations=${resolved.length} duration=${formatDurationMs(durationMsSince(resolveStartedAt))}`
  );
  const validateStartedAt = process.hrtime.bigint();
  validateResolvedOperations(resolved);
  logProgress(
    `selected-modules lower=${runtimeFile} phase=validate_resolved_operations duration=${formatDurationMs(durationMsSince(validateStartedAt))}`
  );
  const finalizeImportsStartedAt = process.hrtime.bigint();
  const resolvedByOwnerId = indexResolvedEntriesByOwnerId(resolved);
  for (const entry of resolved) {
    finalizeResolvedEntryImports(entry, resolvedByOwnerId);
  }
  logProgress(
    `selected-modules lower=${runtimeFile} phase=finalize_entry_imports duration=${formatDurationMs(durationMsSince(finalizeImportsStartedAt))}`
  );

  const runtimeBody = ast.program.body;
  const runtimeRewriteStartedAt = process.hrtime.bigint();
  const moduleFiles = new Map();
  const replacementRuns = [];
  for (const entry of resolved) {
    for (const run of buildRuntimeReplacementRuns(entry)) {
      replacementRuns.push(run);
    }
  }
  const replacementGroups = groupRuntimeReplacementRuns(replacementRuns);
  replacementGroups.sort((left, right) => right.startOrdinal - left.startOrdinal || right.endOrdinal - left.endOrdinal);
  for (const group of replacementGroups) {
    runtimeBody.splice(
      group.startOrdinal,
      group.endOrdinal - group.startOrdinal + 1,
      ...group.runs.flatMap(buildRuntimeReplacementStatements)
    );
  }

  if (resolved.length > 0) {
    const importInsertIndex = countLeadingImports(runtimeBody);
    runtimeBody.splice(importInsertIndex, 0, ...resolved.map(buildRuntimeImportDeclaration));
  }
  logProgress(
    `selected-modules lower=${runtimeFile} phase=rewrite_runtime_body duration=${formatDurationMs(durationMsSince(runtimeRewriteStartedAt))}`
  );

  const runtimeRenameStartedAt = process.hrtime.bigint();
  applyFinalBindingRenamesToGeneratedFile(ast, buildRuntimeBindingRenames(resolved), {
    context: `${runtimeFile} runtime lowering`,
  });
  logProgress(
    `selected-modules lower=${runtimeFile} phase=rename_runtime_bindings duration=${formatDurationMs(durationMsSince(runtimeRenameStartedAt))}`
  );

  const jsFiles = new Map([[runtimeFile, { ast, headerLines }]]);
  const applied = [];
  const buildModulesStartedAt = process.hrtime.bigint();
  const buildModulePhaseDurationsMs = Object.create(null);
  for (const entry of resolved) {
    const moduleFile = buildExtractedModuleFile(entry, { phaseDurationsMs: buildModulePhaseDurationsMs });
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
  const buildModulesDurationMs = durationMsSince(buildModulesStartedAt);
  logProgress(
    `selected-modules lower=${runtimeFile} phase=build_extracted_modules modules=${resolved.length} duration=${formatDurationMs(buildModulesDurationMs)}`
  );
  logProgress(
    `selected-modules lower=${runtimeFile} phase=build_extracted_modules_breakdown imports=${formatDurationMs(buildModulePhaseDurationsMs.imports ?? 0)} body=${formatDurationMs(buildModulePhaseDurationsMs.body ?? 0)} rewrite=${formatDurationMs(buildModulePhaseDurationsMs.rewrite ?? 0)} ast=${formatDurationMs(buildModulePhaseDurationsMs.ast ?? 0)} rename=${formatDurationMs(buildModulePhaseDurationsMs.rename ?? 0)}`
  );
  logProgress(
    `selected-modules lower=${runtimeFile} phase=done duration=${formatDurationMs(durationMsSince(loweringStartedAt))}`
  );

  return {
    analysis: runtimeAnalysis,
    applied,
    jsFiles,
    modules: moduleFiles,
  };
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
  const result = lowerSelectedModuleRegionsInAst(ast, operations, {
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
    runtimeFile,
    sideEffectById,
    topLevelNames,
  }
) {
  validateExtractOperationShape(operation);
  if (operation.selector.file && normalizeRelativeFile(operation.selector.file) !== runtimeFile) {
    throw new Error(`Extract operation ${operation.id} targets ${operation.selector.file}, expected ${runtimeFile}`);
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
  const ownerFragmentsByOwnerId = buildOwnerFragmentsByOwnerId(operation.selector.ownerFragments ?? [], operation.id);
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

  const ownerEntries = orderedOwners.flatMap((owner) => {
    const fragments = ownerFragmentsByOwnerId.get(owner.id);
    if (!fragments || fragments.length === 0) {
      return [
        {
          kind: "declaration",
          owner,
          statement: programBody[owner.ordinal],
        },
      ];
    }
    return fragments.map((fragment) => ({
      fragment,
      kind: "declaration",
      owner,
      statement: programBody[owner.ordinal],
    }));
  });
  const attachedEntries = attachedSideEffects
    .map((sideEffect) => ({
      kind: "side_effect",
      sideEffect,
      statement: programBody[sideEffect.ordinal],
    }))
    .sort((left, right) => left.sideEffect.ordinal - right.sideEffect.ordinal);
  const exportedNames = collectSelectedEntryExportNames(ownerEntries);
  const bindingPlacements = finalizeBindingPlacements(operation.bindingPlacements ?? [], operation.id);
  const exportBindings = finalizeExportBindings(exportedNames, bindingPlacements, operation.id);
  if (exportedNames.includes(initName)) {
    throw new Error(`Extract operation ${operation.id} target.init ${initName} conflicts with an extracted binding`);
  }
  if (topLevelNames.has(initName)) {
    throw new Error(`Extract operation ${operation.id} target.init ${initName} already exists at top level`);
  }

  const usedRuntimeImportLocals = new Set();
  const usedExtractedDependencyNames = new Map();
  const selectedBindingCoverage = buildSelectedBindingCoverage(ownerEntries);
  for (const ownerEntry of ownerEntries) {
    const selectedAccessRecord =
      ownerEntry.fragment && ownerEntry.owner.type === "VariableDeclaration"
        ? analyzeVariableDeclarationFragmentAccesses(ownerEntry.statement, ownerEntry.fragment, {
            owners: analysis.owners,
            runtimeImports: analysis.runtimeImports,
          })
        : ownerEntry.owner;
    validateSelectedOwner(ownerEntry.owner, {
      extractedOwnerToOperation,
      operation,
      selectedAccessRecord,
      ownerFragmentSelected: Boolean(ownerEntry.fragment),
      ownerById,
      selectedBindingCoverage,
      selectedOwnerIds,
      selectedFunctionIds,
      selectedLocalNames: new Set(ownerEntry.fragment?.memberNames ?? topLevelDeclarationNames(ownerEntry.statement)),
      statementNode: ownerEntry.statement,
      usedExtractedDependencyNames,
      usedRuntimeImportLocals,
    });
  }
  for (const sideEffect of attachedSideEffects) {
    validateAttachedSideEffect(sideEffect, {
      extractedOwnerToOperation,
      operation,
      ownerById,
      selectedBindingCoverage,
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
    remainingProgramValidationIndex,
    selectedOwnerIds,
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

  const plainImportEligible = isPlainImportEligibleEntry({
    attachedEntries,
    orderedOwners,
    ownerEntries,
    stageRuns,
  });
  const naturalizedDeclarationEntries = plainImportEligible
    ? []
    : buildNaturalizedDeclarationEntries(stageRuns, {
        remainingProgramValidationIndex,
      });

  return {
    endOrdinal,
    exportedNames,
    exportBindings,
    bindingPlacements,
    id: operation.id,
    initName,
    lowering,
    operation: operation.operation,
    atomicBoundaryUnits: normalizeAtomicBoundaryUnits(operation.atomicBoundaryUnits ?? []),
    attachedEntries,
    ownerEntries,
    orderedOwners,
    ownerIds: orderedOwners.map((owner) => owner.id),
    naturalizedDeclarationEntries,
    naturalizedDeclarationKeys: new Set(naturalizedDeclarationEntries.map(ownerEntryBoundaryKey)),
    plainImportEligible,
    stageRuns,
    startOrdinal,
    targetFile,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    usedRuntimeImports: materializeUsedRuntimeImports(runtimeImportIndex, usedRuntimeImportLocals),
  };
}

function isPlainImportEligibleEntry({ attachedEntries, orderedOwners, ownerEntries, stageRuns }) {
  if (attachedEntries.length > 0) {
    return false;
  }
  const stageEntries = buildCanonicalPlainImportStageEntries(stageRuns);
  if (stageEntries.some((entry) => entry.kind !== "declaration")) {
    return false;
  }
  const safetyContext = buildPlainImportSafetyContext(stageEntries);
  for (const stageEntry of stageEntries) {
    if (!isPlainImportEligibleDeclarationEntry(stageEntry, ownerEntries, safetyContext)) {
      return false;
    }
    recordPlainImportSafeProvider(stageEntry, safetyContext);
  }
  return true;
}

function buildNaturalizedDeclarationEntries(stageRuns, { remainingProgramValidationIndex }) {
  const naturalizedEntries = [];
  const safetyContext = { safeProviderBindings: new Set() };
  const stageEntries = buildCanonicalPlainImportStageEntries(stageRuns);
  for (const stageEntry of stageEntries) {
    if (
      !isNaturalizableDeclarationEntry(stageEntry, {
        remainingProgramValidationIndex,
        safetyContext,
      })
    ) {
      continue;
    }
    naturalizedEntries.push(stageEntry);
    recordPlainImportSafeProvider(stageEntry, safetyContext);
  }
  return naturalizedEntries;
}

function buildCanonicalPlainImportStageEntries(stageRuns) {
  const entries = [];
  const seenKeys = new Set();
  for (const stageRun of stageRuns) {
    for (const stageEntry of stageRun.stageEntries) {
      const key =
        stageEntry.kind === "declaration"
          ? ownerEntryBoundaryKey(stageEntry)
          : (stageEntry.sideEffect?.id ?? `${stageEntry.kind}:${entries.length}`);
      if (seenKeys.has(key)) {
        continue;
      }
      seenKeys.add(key);
      entries.push(stageEntry);
    }
  }
  return entries;
}

function buildPlainImportSafetyContext(stageEntries) {
  const safeProviderBindings = new Set();
  for (const stageEntry of stageEntries) {
    if (stageEntry.kind !== "declaration" || stageEntry.owner.type !== "FunctionDeclaration") {
      continue;
    }
    for (const name of selectedEntryBindingNames(stageEntry)) {
      safeProviderBindings.add(providerBindingKey(stageEntry.owner.id, name));
    }
  }
  return {
    safeProviderBindings,
  };
}

function recordPlainImportSafeProvider(stageEntry, safetyContext) {
  if (!safetyContext || stageEntry.kind !== "declaration") {
    return;
  }
  for (const name of selectedEntryBindingNames(stageEntry)) {
    safetyContext.safeProviderBindings.add(providerBindingKey(stageEntry.owner.id, name));
  }
}

function selectedEntryBindingNames(stageEntry) {
  return stageEntry.fragment?.memberNames ?? topLevelDeclarationNames(stageEntry.statement);
}

function providerBindingKey(ownerId, name) {
  return `${ownerId}:${name}`;
}

function isPlainImportEligibleDeclarationEntry(stageEntry, ownerEntries, safetyContext = null) {
  const { owner } = stageEntry;
  if (owner.type === "FunctionDeclaration") {
    return true;
  }
  if (owner.type === "ClassDeclaration") {
    return isPlainImportSafeClassOwner(owner, stageEntry.statement, safetyContext);
  }
  if (owner.type !== "VariableDeclaration") {
    return false;
  }
  return isPlainImportSafeVariableEntry(stageEntry, ownerEntries);
}

function isNaturalizableDeclarationEntry(stageEntry, { remainingProgramValidationIndex, safetyContext }) {
  if (stageEntry.kind !== "declaration" || stageEntry.fragment) {
    return false;
  }
  if (hasEarlierPotentialBindingUse(stageEntry, remainingProgramValidationIndex)) {
    return false;
  }
  if (stageEntry.owner.type === "FunctionDeclaration") {
    return t.isFunctionDeclaration(stageEntry.statement);
  }
  if (stageEntry.owner.type === "ClassDeclaration") {
    return (
      t.isClassDeclaration(stageEntry.statement) &&
      isPlainImportSafeClassOwner(stageEntry.owner, stageEntry.statement, safetyContext)
    );
  }
  if (stageEntry.owner.type !== "VariableDeclaration") {
    return false;
  }
  const shape = declarationShapedVariableEntry(stageEntry);
  if (!shape) {
    return false;
  }
  if (!hasNoOwnerTopLevelWrites(stageEntry.owner)) {
    return false;
  }
  if (shape.kind === "function") {
    return eagerAccessRecords(stageEntry.owner, "readsTopLevel", "eager").length === 0;
  }
  return isPlainImportSafeClassExpressionOwner(stageEntry.owner, shape.expression, safetyContext);
}

function declarationShapedVariableEntry(stageEntry) {
  const { statement } = stageEntry;
  if (!t.isVariableDeclaration(statement) || statement.declarations.length !== 1) {
    return null;
  }
  const declaration = statement.declarations[0];
  if (!t.isIdentifier(declaration.id)) {
    return null;
  }
  if (t.isFunctionExpression(declaration.init)) {
    return {
      bindingName: declaration.id.name,
      declaration,
      expression: declaration.init,
      kind: "function",
      statement,
    };
  }
  if (t.isClassExpression(declaration.init)) {
    return {
      bindingName: declaration.id.name,
      declaration,
      expression: declaration.init,
      kind: "class",
      statement,
    };
  }
  return null;
}

function hasEarlierPotentialBindingUse(stageEntry, remainingProgramValidationIndex) {
  for (const name of selectedEntryBindingNames(stageEntry)) {
    for (const record of remainingProgramValidationIndex.potentialUsersByOwnerId.get(stageEntry.owner.id) ?? []) {
      if (
        record.name === name &&
        record.ordinal < stageEntry.owner.ordinal &&
        record.recordId !== stageEntry.owner.id
      ) {
        return true;
      }
    }
    for (const record of remainingProgramValidationIndex.writersByOwnerId.get(stageEntry.owner.id) ?? []) {
      if (
        record.name === name &&
        record.ordinal < stageEntry.owner.ordinal &&
        record.recordId !== stageEntry.owner.id
      ) {
        return true;
      }
    }
  }
  return false;
}

function hasNoOwnerTopLevelWrites(owner) {
  return (
    eagerAccessRecords(owner, "writesTopLevel", "eager").length === 0 &&
    eagerAccessRecords(owner, "writesTopLevel", "lazy").length === 0
  );
}

function isPlainImportSafeClassExpressionOwner(owner, expression, safetyContext) {
  const classFeatures = describeClassExpressionFeatures(expression);
  const eagerReads = eagerAccessRecords(owner, "readsTopLevel", "eager");
  return (
    classFeatures.staticBlockCount === 0 &&
    classFeatures.staticFieldCount === 0 &&
    classFeatures.computedKeyCount === 0 &&
    eagerAccessRecords(owner, "memberWritesTopLevel", "eager").length === 0 &&
    isPlainImportSafeClassExpressionSuperClass(expression, eagerReads, safetyContext) &&
    eagerReads.every((access) => isPlainImportSafeClassExpressionEagerRead(access, expression, safetyContext))
  );
}

function isPlainImportSafeClassExpressionSuperClass(expression, eagerReads, safetyContext) {
  if (!expression.superClass) {
    return true;
  }
  if (!t.isIdentifier(expression.superClass)) {
    return false;
  }
  return eagerReads.some(
    (access) =>
      access.name === expression.superClass.name &&
      isPlainImportSafeClassExpressionEagerRead(access, expression, safetyContext)
  );
}

function isPlainImportSafeClassExpressionEagerRead(access, expression, safetyContext) {
  if (!expression.superClass || access.name !== expression.superClass.name) {
    return false;
  }
  if (access.kind === "runtime_import") {
    return true;
  }
  if (access.kind !== "local_declaration" || !access.ownerId || !access.name || !safetyContext) {
    return false;
  }
  return safetyContext.safeProviderBindings.has(providerBindingKey(access.ownerId, access.name));
}

function describeClassExpressionFeatures(expression) {
  const features = {
    computedKeyCount: 0,
    staticBlockCount: 0,
    staticFieldCount: 0,
  };
  if (!t.isClassExpression(expression) || expression.decorators?.length > 0) {
    features.computedKeyCount = Number.POSITIVE_INFINITY;
    return features;
  }
  for (const member of expression.body.body) {
    if (t.isStaticBlock(member)) {
      features.staticBlockCount += 1;
      continue;
    }
    if ((t.isClassProperty(member) || t.isClassPrivateProperty(member)) && member.static) {
      features.staticFieldCount += 1;
    }
    if (member.computed) {
      features.computedKeyCount += 1;
    }
  }
  return features;
}

function isPlainImportSafeClassOwner(owner, statement, safetyContext = null) {
  const classFeatures = owner.classFeatures ?? {
    hasSuperClass: false,
    staticBlockCount: 0,
    staticFieldCount: 0,
    computedKeyCount: 0,
  };
  const eagerReads = eagerAccessRecords(owner, "readsTopLevel", "eager");
  return (
    classFeatures.staticBlockCount === 0 &&
    classFeatures.staticFieldCount === 0 &&
    classFeatures.computedKeyCount === 0 &&
    eagerAccessRecords(owner, "writesTopLevel", "eager").length === 0 &&
    eagerAccessRecords(owner, "memberWritesTopLevel", "eager").length === 0 &&
    isPlainImportSafeClassSuperClass(owner, statement, eagerReads, safetyContext) &&
    eagerReads.every((access) => isPlainImportSafeClassEagerRead(access, safetyContext))
  );
}

function isPlainImportSafeClassSuperClass(owner, statement, eagerReads, safetyContext) {
  if (!owner.classFeatures?.hasSuperClass) {
    return true;
  }
  if (!t.isClassDeclaration(statement) || !t.isIdentifier(statement.superClass)) {
    return false;
  }
  return eagerReads.some(
    (access) => access.name === statement.superClass.name && isPlainImportSafeClassEagerRead(access, safetyContext)
  );
}

function isPlainImportSafeClassEagerRead(access, safetyContext) {
  if (!access.siteKinds?.every((siteKind) => siteKind === "class_superclass")) {
    return false;
  }
  if (access.kind === "runtime_import") {
    return true;
  }
  if (access.kind !== "local_declaration" || !access.ownerId || !access.name || !safetyContext) {
    return false;
  }
  return safetyContext.safeProviderBindings.has(providerBindingKey(access.ownerId, access.name));
}

function eagerAccessRecords(owner, bucketName, phase) {
  const finalizedBucket = owner[bucketName]?.[phase];
  if (Array.isArray(finalizedBucket)) {
    return finalizedBucket;
  }
  const legacyMapName = legacyAccessMapName(bucketName, phase);
  const legacyMap = owner[legacyMapName];
  return legacyMap?.values ? [...legacyMap.values()] : [];
}

function legacyAccessMapName(bucketName, phase) {
  const prefix = phase === "eager" ? "eager" : "lazy";
  if (bucketName === "readsTopLevel") {
    return `${prefix}Reads`;
  }
  if (bucketName === "writesTopLevel") {
    return `${prefix}Writes`;
  }
  return `${prefix}MemberWrites`;
}

function isPlainImportSafeVariableEntry(stageEntry, ownerEntries) {
  const { owner, statement, fragment } = stageEntry;
  if (!t.isVariableDeclaration(statement)) {
    return false;
  }
  const declarations = fragment
    ? fragment.declaratorIndices.map((index) => statement.declarations[index]).filter(Boolean)
    : statement.declarations;
  if (declarations.length === 0) {
    return false;
  }
  if (fragment) {
    return supportsDirectFragmentVariableLowering(fragment) && declarations.every(isPlainImportSafeVariableDeclarator);
  }
  if (ownerEntries.some((entry) => entry.owner.id === owner.id && entry.fragment)) {
    return false;
  }
  if (isPlainImportSafeSnapshotVariableEntry(stageEntry)) {
    return true;
  }
  return (
    (owner.eagerReads?.size ?? 0) === 0 &&
    (owner.eagerWrites?.size ?? 0) === 0 &&
    (owner.eagerMemberWrites?.size ?? 0) === 0 &&
    declarations.every(isPlainImportSafeVariableDeclarator)
  );
}

function isPlainImportSafeSnapshotVariableEntry(stageEntry) {
  const { owner, statement, fragment } = stageEntry;
  if (
    fragment ||
    owner.currentExtractorLowering !== "snapshot_variable_declaration" ||
    !t.isVariableDeclaration(statement) ||
    statement.kind !== "var"
  ) {
    return false;
  }
  const declaredNameSet = new Set(statement.declarations.flatMap((declaration) => bindingNames(declaration.id)));
  if (declaredNameSet.size === 0) {
    return false;
  }
  return statement.declarations.every((declaration) =>
    referencedUndeclaredNamesInVariableDeclarator(declaration).every((name) => declaredNameSet.has(name))
  );
}

function isPlainImportSafeVariableDeclarator(declaration) {
  return isPlainImportSafeBindingPattern(declaration.id) && isPlainImportSafeExpression(declaration.init);
}

function isPlainImportSafeBindingPattern(pattern) {
  if (!pattern) {
    return false;
  }
  if (t.isIdentifier(pattern)) {
    return true;
  }
  if (t.isAssignmentPattern(pattern)) {
    return isPlainImportSafeBindingPattern(pattern.left) && isPlainImportSafeExpression(pattern.right);
  }
  if (t.isRestElement(pattern)) {
    return isPlainImportSafeBindingPattern(pattern.argument);
  }
  if (t.isArrayPattern(pattern)) {
    return pattern.elements.every((element) => element == null || isPlainImportSafeBindingPattern(element));
  }
  if (t.isObjectPattern(pattern)) {
    return pattern.properties.every((property) => {
      if (t.isRestElement(property)) {
        return isPlainImportSafeBindingPattern(property.argument);
      }
      if (!t.isObjectProperty(property) || property.computed) {
        return false;
      }
      return isPlainImportSafeBindingPattern(property.value);
    });
  }
  return false;
}

function isPlainImportSafeExpression(node) {
  if (!node) {
    return true;
  }
  if (
    t.isIdentifier(node) ||
    t.isStringLiteral(node) ||
    t.isNumericLiteral(node) ||
    t.isBooleanLiteral(node) ||
    t.isNullLiteral(node) ||
    t.isBigIntLiteral(node) ||
    t.isRegExpLiteral(node)
  ) {
    return true;
  }
  if (t.isTemplateLiteral(node)) {
    return node.expressions.every(isPlainImportSafeExpression);
  }
  if (t.isUnaryExpression(node)) {
    return node.operator !== "delete" && isPlainImportSafeExpression(node.argument);
  }
  if (t.isBinaryExpression(node) || t.isLogicalExpression(node)) {
    return isPlainImportSafeExpression(node.left) && isPlainImportSafeExpression(node.right);
  }
  if (t.isConditionalExpression(node)) {
    return (
      isPlainImportSafeExpression(node.test) &&
      isPlainImportSafeExpression(node.consequent) &&
      isPlainImportSafeExpression(node.alternate)
    );
  }
  if (t.isArrayExpression(node)) {
    return node.elements.every((element) => {
      if (element == null) {
        return true;
      }
      if (t.isSpreadElement(element)) {
        return false;
      }
      return isPlainImportSafeExpression(element);
    });
  }
  if (t.isObjectExpression(node)) {
    return node.properties.every((property) => {
      if (!t.isObjectProperty(property) || property.computed) {
        return false;
      }
      return isPlainImportSafeExpression(property.value);
    });
  }
  return false;
}

function validateSelectedOwner(
  owner,
  {
    extractedOwnerToOperation,
    operation,
    selectedAccessRecord,
    ownerFragmentSelected,
    ownerById,
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedFunctionIds,
    selectedLocalNames,
    statementNode,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
  }
) {
  const accessRecord = selectedAccessRecord ?? owner;
  if (!SUPPORTED_OWNER_TYPES.has(owner.type)) {
    throw new Error(`Extract operation ${operation.id} does not support ${owner.type} owners yet`);
  }
  if (owner.effects.containsDirectEval || owner.effects.containsImportMeta || owner.effects.containsTopLevelAwait) {
    throw new Error(`Extract operation ${operation.id} cannot extract runtime-sensitive owner ${owner.id}`);
  }
  if (owner.currentExtractorCompatible === false) {
    if (owner.type === "VariableDeclaration" && ownerFragmentSelected) {
      // Fragment-aware extraction can legalize otherwise incompatible declaration packs.
    } else {
      throw new Error(
        `Extract operation ${operation.id} cannot extract owner ${owner.id}: ${owner.currentExtractorBlockingReasons.join(",")}`
      );
    }
  }
  if (
    owner.type === "VariableDeclaration" &&
    owner.currentExtractorLowering !== "snapshot_variable_declaration" &&
    !ownerFragmentSelected
  ) {
    validateVariableDeclarators(statementNode, operation.id, owner.id);
  }

  validateSelectedOwnerAccesses(accessRecord.readsTopLevel.eager, "eager read", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: owner.id,
    ownerById,
    ownerOrdinal: owner.ordinal,
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedLocalNames,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: false,
  });
  validateSelectedOwnerAccesses(accessRecord.readsTopLevel.lazy, "lazy read", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: owner.id,
    ownerById,
    ownerOrdinal: owner.ordinal,
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedLocalNames,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: false,
  });
  validateSelectedOwnerAccesses(accessRecord.memberWritesTopLevel.eager, "eager member write", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: owner.id,
    ownerById,
    ownerOrdinal: owner.ordinal,
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedLocalNames,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: true,
  });
  validateSelectedOwnerAccesses(accessRecord.memberWritesTopLevel.lazy, "lazy member write", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: owner.id,
    ownerById,
    ownerOrdinal: owner.ordinal,
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedLocalNames,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: true,
  });
  validateSelectedOwnerAccesses(accessRecord.writesTopLevel.eager, "eager write", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: owner.id,
    ownerById,
    ownerOrdinal: owner.ordinal,
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedLocalNames,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: true,
  });
  validateSelectedOwnerAccesses(accessRecord.writesTopLevel.lazy, "lazy write", {
    extractedOwnerToOperation,
    operationId: operation.id,
    ownerId: owner.id,
    ownerById,
    ownerOrdinal: owner.ordinal,
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedLocalNames,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite: true,
  });

  for (const access of accessRecord.readsTopLevel.eager) {
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
  for (const access of [...accessRecord.writesTopLevel.eager, ...accessRecord.memberWritesTopLevel.eager]) {
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
    selectedBindingCoverage,
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
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedLocalNames: new Set(),
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
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedLocalNames: new Set(),
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
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedLocalNames: new Set(),
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
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedLocalNames: new Set(),
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
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedLocalNames: new Set(),
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
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedLocalNames: new Set(),
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
    selectedBindingCoverage,
    selectedOwnerIds,
    selectedLocalNames,
    usedExtractedDependencyNames,
    usedRuntimeImportLocals,
    allowSelectedLocalWrite,
  }
) {
  for (const access of accesses) {
    const isMemberWrite = accessLabel.includes("member");
    const isWrite = accessLabel.includes("write");
    if (access.kind === "runtime_import") {
      if (isWrite && !isMemberWrite) {
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
      if (currentOperationSelectsBinding(selectedBindingCoverage, access.ownerId, access.name)) {
        if (!allowSelectedLocalWrite && isWrite) {
          throw new Error(
            `Extract operation ${operationId} owner ${ownerId} has unsupported ${accessLabel} to extracted owner ${access.ownerId}`
          );
        }
        continue;
      }
      if (isWrite && !isMemberWrite) {
        throw new Error(
          `Extract operation ${operationId} owner ${ownerId} has unsupported ${accessLabel} to separately extracted binding ${access.name}`
        );
      }
      recordExtractedDependencyName(usedExtractedDependencyNames, access.ownerId, access.name);
      continue;
    }
    if (extractedOwnerToOperation.has(access.ownerId)) {
      if (isWrite && !isMemberWrite) {
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
  { attachedEntries, ownerEntries, ownerById, remainingProgramValidationIndex, selectedOwnerIds }
) {
  const stageItems = [
    ...ownerEntries.map((entry) => ({
      kind: "declaration",
      ordinal: entry.owner.ordinal,
      ownerEntries: [entry],
      sortIndex: entry.fragment?.orderIndex ?? 0,
      statementEntries: [entry],
    })),
    ...attachedEntries.map((entry) => ({
      kind: "side_effect",
      ordinal: entry.sideEffect.ordinal,
      ownerEntries: [],
      sortIndex: Number.MAX_SAFE_INTEGER,
      statementEntries: [entry],
    })),
  ].sort((left, right) => left.ordinal - right.ordinal || left.sortIndex - right.sortIndex);
  const stageRuns = [];
  for (const stageItem of stageItems) {
    const currentStage = stageRuns.at(-1);
    if (!currentStage || currentStage.endOrdinal + 1 !== stageItem.ordinal) {
      stageRuns.push({
        endOrdinal: stageItem.ordinal,
        ownerEntries: [...stageItem.ownerEntries],
        sortIndex: stageItem.sortIndex,
        stageEntries: [...stageItem.statementEntries],
        startOrdinal: stageItem.ordinal,
      });
      continue;
    }
    currentStage.endOrdinal = stageItem.ordinal;
    currentStage.ownerEntries.push(...stageItem.ownerEntries);
    currentStage.sortIndex = Math.min(currentStage.sortIndex, stageItem.sortIndex);
    currentStage.stageEntries.push(...stageItem.statementEntries);
  }

  if (stageRuns.length <= 1) {
    return stageRuns;
  }

  const firstRetainedOrdinal = stageRuns[0].endOrdinal + 1;
  const lastOrdinal = stageRuns.at(-1).endOrdinal;
  const blockedUse = findRetainedEagerUseOfLaterSelectedOwner({
    firstRetainedOrdinal,
    lastOrdinal,
    ownerById,
    remainingProgramValidationIndex,
    selectedOwnerIds,
  });
  if (blockedUse) {
    throw new Error(
      `Extract operation ${operation.id} staged shell item ${blockedUse.recordId} eagerly uses later extracted owner ${blockedUse.targetOwnerId}`
    );
  }

  return stageRuns;
}

function findRetainedEagerUseOfLaterSelectedOwner({
  firstRetainedOrdinal,
  lastOrdinal,
  ownerById,
  remainingProgramValidationIndex,
  selectedOwnerIds,
}) {
  let earliestBlockedUse = null;
  for (const selectedOwnerId of selectedOwnerIds) {
    const targetOwner = ownerById.get(selectedOwnerId);
    if (!targetOwner) {
      continue;
    }
    for (const record of remainingProgramValidationIndex.earlierEagerUsersByOwnerId.get(selectedOwnerId) ?? []) {
      if (record.ordinal < firstRetainedOrdinal || record.ordinal >= lastOrdinal) {
        continue;
      }
      if (selectedOwnerIds.has(record.recordId) || targetOwner.ordinal <= record.ordinal) {
        continue;
      }
      if (!earliestBlockedUse || record.ordinal < earliestBlockedUse.ordinal) {
        earliestBlockedUse = {
          ordinal: record.ordinal,
          recordId: record.recordId,
          targetOwnerId: selectedOwnerId,
        };
      }
    }
  }
  return earliestBlockedUse;
}

function validateResolvedOperations(resolved) {
  const targetFiles = new Set();
  const initNames = new Set();
  const ownerCoverageById = new Map();
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
    for (const ownerEntry of entry.ownerEntries) {
      const coverage = ownerCoverageById.get(ownerEntry.owner.id) ?? {
        fragmentIds: new Set(),
        fullOwner: false,
      };
      if (!ownerEntry.fragment) {
        if (coverage.fullOwner || coverage.fragmentIds.size > 0) {
          throw new Error(`Overlapping extraction regions include owner ${ownerEntry.owner.id}`);
        }
        coverage.fullOwner = true;
        ownerCoverageById.set(ownerEntry.owner.id, coverage);
        continue;
      }
      if (coverage.fullOwner || coverage.fragmentIds.has(ownerEntry.fragment.id)) {
        throw new Error(`Overlapping extraction regions include owner fragment ${ownerEntry.fragment.id}`);
      }
      coverage.fragmentIds.add(ownerEntry.fragment.id);
      ownerCoverageById.set(ownerEntry.owner.id, coverage);
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
    ...entry.exportBindings.map((binding) => importSpecifierForLocal(binding.local, binding.exported, "named")),
    ...runtimeInitNamesForEntry(entry).map((name) => importSpecifierForLocal(name, name, "named")),
  ];
  return addSelectedModuleLoweringNodeComment(
    t.importDeclaration(specifiers, t.stringLiteral(runtimeImportSourceForTarget(entry.targetFile)))
  );
}

function buildRuntimeReplacementRuns(entry) {
  if (entry.plainImportEligible) {
    return entry.stageRuns.map((stageRun, stageIndex) => ({
      endOrdinal: stageRun.endOrdinal,
      entry,
      omitInitCall: true,
      sortIndex: stageRun.sortIndex ?? stageIndex,
      stageIndex,
      startOrdinal: stageRun.startOrdinal,
    }));
  }
  return entry.stageRuns.map((stageRun, stageIndex) => ({
    endOrdinal: stageRun.endOrdinal,
    entry,
    omitInitCall: !stageRunNeedsInitWork(entry, stageRun),
    sortIndex: stageRun.sortIndex ?? stageIndex,
    stageIndex,
    startOrdinal: stageRun.startOrdinal,
  }));
}

function groupRuntimeReplacementRuns(runs) {
  const sortedRuns = [...runs].sort(
    (left, right) =>
      left.startOrdinal - right.startOrdinal || right.endOrdinal - left.endOrdinal || compareRuntimeRuns(left, right)
  );
  const groups = [];
  for (const run of sortedRuns) {
    const currentGroup = groups.at(-1);
    if (!currentGroup || run.startOrdinal > currentGroup.endOrdinal) {
      groups.push({
        endOrdinal: run.endOrdinal,
        runs: [run],
        startOrdinal: run.startOrdinal,
      });
      continue;
    }
    currentGroup.endOrdinal = Math.max(currentGroup.endOrdinal, run.endOrdinal);
    currentGroup.runs.push(run);
  }
  return groups.map((group) => ({
    ...group,
    runs: sortRuntimeRunsWithinGroup(group.runs),
  }));
}

function compareRuntimeRuns(left, right) {
  const leftSpan = left.endOrdinal - left.startOrdinal;
  const rightSpan = right.endOrdinal - right.startOrdinal;
  return (
    leftSpan - rightSpan ||
    left.sortIndex - right.sortIndex ||
    left.stageIndex - right.stageIndex ||
    left.entry.id.localeCompare(right.entry.id)
  );
}

function sortRuntimeRunsWithinGroup(runs) {
  const outgoing = new Map(runs.map((run) => [run, new Set()]));
  const incomingCount = new Map(runs.map((run) => [run, 0]));
  const runsByTargetFile = new Map();
  for (const run of runs) {
    if (!runsByTargetFile.has(run.entry.targetFile)) {
      runsByTargetFile.set(run.entry.targetFile, []);
    }
    runsByTargetFile.get(run.entry.targetFile).push(run);
  }

  for (const providerRuns of runsByTargetFile.values()) {
    providerRuns.sort((left, right) => left.stageIndex - right.stageIndex || compareRuntimeRuns(left, right));
    for (let index = 1; index < providerRuns.length; index++) {
      addRuntimeRunDependency(providerRuns[index - 1], providerRuns[index], { outgoing, incomingCount });
    }
  }

  for (const consumerRun of runs) {
    for (const importRecord of consumerRun.entry.usedExtractedImports ?? []) {
      for (const providerRun of runsByTargetFile.get(importRecord.sourceTargetFile) ?? []) {
        addRuntimeRunDependency(providerRun, consumerRun, { outgoing, incomingCount });
      }
    }
  }

  const ready = runs.filter((run) => incomingCount.get(run) === 0).sort(compareRuntimeRuns);
  const ordered = [];
  while (ready.length > 0) {
    const nextRun = ready.shift();
    ordered.push(nextRun);
    const neighbours = [...(outgoing.get(nextRun) ?? [])].sort(compareRuntimeRuns);
    for (const neighbour of neighbours) {
      const nextIncomingCount = (incomingCount.get(neighbour) ?? 0) - 1;
      incomingCount.set(neighbour, nextIncomingCount);
      if (nextIncomingCount === 0) {
        ready.push(neighbour);
        ready.sort(compareRuntimeRuns);
      }
    }
  }

  if (ordered.length !== runs.length) {
    return [...runs].sort(compareRuntimeRuns);
  }
  return ordered;
}

function addRuntimeRunDependency(providerRun, consumerRun, { outgoing, incomingCount }) {
  if (providerRun === consumerRun) {
    return;
  }
  const providerOutgoing = outgoing.get(providerRun);
  if (!providerOutgoing || providerOutgoing.has(consumerRun)) {
    return;
  }
  providerOutgoing.add(consumerRun);
  incomingCount.set(consumerRun, (incomingCount.get(consumerRun) ?? 0) + 1);
}

function buildInitCallStatement(run) {
  return addSelectedModuleLoweringNodeComment(
    t.expressionStatement(t.callExpression(t.identifier(runtimeInitNameForRun(run)), []))
  );
}

function buildRuntimeReplacementStatements(run) {
  return run.omitInitCall ? [] : [buildInitCallStatement(run)];
}

function buildExtractedModuleFile(entry, { phaseDurationsMs = null } = {}) {
  if (entry.plainImportEligible) {
    return buildPlainImportModuleFile(entry, { phaseDurationsMs });
  }
  const needsRuntimeSourceRewrite = entryNeedsRuntimeSourceRewrite(entry);
  const importsStartedAt = phaseDurationsMs ? process.hrtime.bigint() : null;
  const body = [];
  for (const importRecord of entry.usedExtractedImports ?? []) {
    body.push(importDeclarationFromExtractedImportRecord(importRecord, entry.targetFile));
  }
  for (const importRecord of entry.usedRuntimeImports) {
    body.push(importDeclarationFromRuntimeImportRecord(importRecord, entry.targetFile));
  }
  if (importsStartedAt) {
    addDurationMs(phaseDurationsMs, "imports", durationMsSince(importsStartedAt));
  }
  const bodyStartedAt = phaseDurationsMs ? process.hrtime.bigint() : null;
  const entryBindingRenames = buildEntryBindingRenames(entry);
  const localRenameMap = buildBindingRenameMap(entryBindingRenames);
  const naturalizedBindingNames = naturalizedEntryBindingNameSet(entry);
  const trivialAliasDeclarations = [];
  for (const stageRun of entry.stageRuns) {
    for (const stageEntry of stageRun.stageEntries) {
      const declaration = trivialAliasDeclaration(stageEntry);
      if (declaration) {
        trivialAliasDeclarations.push(declaration);
      }
    }
  }
  const initializedExportBindings = entry.exportBindings.filter(
    (binding) =>
      !naturalizedBindingNames.has(binding.local) &&
      !trivialAliasDeclarations.some(
        (declaration) =>
          t.isIdentifier(declaration.declarations[0]?.id) && declaration.declarations[0].id.name === binding.local
      )
  );
  body.push(...buildNaturalizedDeclarationStatements(entry));
  body.push(...trivialAliasDeclarations);
  if (initializedExportBindings.length > 0) {
    body.push(
      t.variableDeclaration(
        "let",
        initializedExportBindings.map((binding) => t.variableDeclarator(t.identifier(binding.local)))
      )
    );
  }
  for (const stage of moduleStagesForEntry(entry)) {
    body.push(
      t.exportNamedDeclaration(
        t.functionDeclaration(
          t.identifier(stage.initName),
          [],
          t.blockStatement(
            buildInitStatements(stage.stageEntries, localRenameMap, entry.atomicBoundaryUnits ?? [], {
              naturalizedDeclarationKeys: entry.naturalizedDeclarationKeys,
            })
          )
        )
      )
    );
  }
  body.push(
    t.exportNamedDeclaration(
      null,
      entry.exportBindings.map((binding) =>
        t.exportSpecifier(t.identifier(binding.local), exportNameNode(binding.exported))
      )
    )
  );
  upgradeSingleAssignmentLetsToConst(body);
  if (bodyStartedAt) {
    addDurationMs(phaseDurationsMs, "body", durationMsSince(bodyStartedAt));
  }
  const rewriteStartedAt = phaseDurationsMs ? process.hrtime.bigint() : null;
  if (needsRuntimeSourceRewrite) {
    rewriteStatementsForTarget(body, entry.targetFile);
  }
  if (rewriteStartedAt) {
    addDurationMs(phaseDurationsMs, "rewrite", durationMsSince(rewriteStartedAt));
  }
  const astStartedAt = phaseDurationsMs ? process.hrtime.bigint() : null;
  const ast = t.file(t.program(body));
  if (astStartedAt) {
    addDurationMs(phaseDurationsMs, "ast", durationMsSince(astStartedAt));
  }
  const renameStartedAt = phaseDurationsMs ? process.hrtime.bigint() : null;
  applyFinalBindingRenamesToGeneratedFile(ast, entryBindingRenames, {
    context: `${entry.targetFile} selected-module lowering`,
  });
  if (renameStartedAt) {
    addDurationMs(phaseDurationsMs, "rename", durationMsSince(renameStartedAt));
  }
  return {
    ast,
    headerLines: buildSelectedModuleLoweringHeaderLines(entry.ownerIds),
  };
}

export function upgradeSingleAssignmentLetsToConst(body) {
  const letDeclaration = body.find((statement) => t.isVariableDeclaration(statement) && statement.kind === "let");
  if (!letDeclaration) {
    return;
  }
  const stageFunctions = body.filter(
    (statement) => t.isExportNamedDeclaration(statement) && t.isFunctionDeclaration(statement.declaration)
  );
  if (stageFunctions.length === 0) {
    return;
  }

  const writesByName = collectWriteOccurrences(body);
  const assignmentsByName = new Map();
  for (const stageExport of stageFunctions) {
    for (const statement of stageExport.declaration.body.body) {
      if (!t.isExpressionStatement(statement) || !t.isAssignmentExpression(statement.expression, { operator: "=" })) {
        continue;
      }
      const { left, right } = statement.expression;
      if (!t.isIdentifier(left)) {
        continue;
      }
      const records = assignmentsByName.get(left.name) ?? [];
      records.push({ statement, right });
      assignmentsByName.set(left.name, records);
    }
  }

  const constDeclarators = [];
  const remainingDeclarators = [];
  for (const declarator of letDeclaration.declarations) {
    if (!t.isIdentifier(declarator.id)) {
      remainingDeclarators.push(declarator);
      continue;
    }
    const name = declarator.id.name;
    const assignments = assignmentsByName.get(name) ?? [];
    if (assignments.length !== 1 || (writesByName.get(name) ?? 0) !== 1) {
      remainingDeclarators.push(declarator);
      continue;
    }
    const [{ statement, right }] = assignments;
    if (!isConstEligibleInitializer(right)) {
      remainingDeclarators.push(declarator);
      continue;
    }
    constDeclarators.push(t.variableDeclarator(t.identifier(name), t.cloneNode(right, true)));
    removeStatementFromStageBody(statement, stageFunctions);
  }

  if (constDeclarators.length === 0) {
    return;
  }
  const letIndex = body.indexOf(letDeclaration);
  const replacement = [t.variableDeclaration("const", constDeclarators)];
  if (remainingDeclarators.length > 0) {
    replacement.push(t.variableDeclaration("let", remainingDeclarators));
  }
  body.splice(letIndex, 1, ...replacement);
}

function collectWriteOccurrences(body) {
  const counts = new Map();
  for (const statement of body) {
    collectWriteOccurrencesInNode(statement, counts);
  }
  return counts;
}

function collectWriteOccurrencesInNode(node, counts) {
  if (!node) {
    return;
  }
  if (t.isAssignmentExpression(node) && t.isIdentifier(node.left)) {
    counts.set(node.left.name, (counts.get(node.left.name) ?? 0) + 1);
  }
  if (t.isUpdateExpression(node) && t.isIdentifier(node.argument)) {
    counts.set(node.argument.name, (counts.get(node.argument.name) ?? 0) + 1);
  }
  if ((t.isForInStatement(node) || t.isForOfStatement(node)) && t.isIdentifier(node.left)) {
    counts.set(node.left.name, (counts.get(node.left.name) ?? 0) + 1);
  }
  const keys = t.VISITOR_KEYS[node.type] ?? [];
  for (const key of keys) {
    const value = node[key];
    if (Array.isArray(value)) {
      for (const child of value) {
        collectWriteOccurrencesInNode(child, counts);
      }
    } else {
      collectWriteOccurrencesInNode(value, counts);
    }
  }
}

function removeStatementFromStageBody(targetStatement, stageFunctions) {
  for (const stageExport of stageFunctions) {
    const block = stageExport.declaration.body.body;
    const idx = block.indexOf(targetStatement);
    if (idx >= 0) {
      block.splice(idx, 1);
      return;
    }
  }
}

function isConstEligibleInitializer(node) {
  return (
    t.isLiteral(node) ||
    (t.isUnaryExpression(node) && isConstEligibleInitializer(node.argument)) ||
    (t.isArrayExpression(node) && node.elements.every((el) => el && isConstEligibleInitializer(el))) ||
    (t.isObjectExpression(node) &&
      node.properties.every(
        (prop) =>
          t.isObjectProperty(prop) &&
          (!prop.computed || t.isLiteral(prop.key)) &&
          isConstEligibleInitializer(prop.value)
      ))
  );
}

function buildPlainImportModuleFile(entry, { phaseDurationsMs = null } = {}) {
  const needsRuntimeSourceRewrite = entryNeedsRuntimeSourceRewrite(entry);
  const importsStartedAt = phaseDurationsMs ? process.hrtime.bigint() : null;
  const body = [];
  for (const importRecord of entry.usedExtractedImports ?? []) {
    body.push(importDeclarationFromExtractedImportRecord(importRecord, entry.targetFile));
  }
  for (const importRecord of entry.usedRuntimeImports) {
    body.push(importDeclarationFromRuntimeImportRecord(importRecord, entry.targetFile));
  }
  if (importsStartedAt) {
    addDurationMs(phaseDurationsMs, "imports", durationMsSince(importsStartedAt));
  }
  const bodyStartedAt = phaseDurationsMs ? process.hrtime.bigint() : null;
  const entryBindingRenames = buildEntryBindingRenames(entry);
  const atomicBoundaryIndex = buildAtomicBoundaryIndex(entry.atomicBoundaryUnits ?? []);
  let previousAtomicBoundaryUnitId = null;
  for (const stageEntry of buildCanonicalPlainImportStageEntries(entry.stageRuns)) {
    if (stageEntry.kind !== "declaration") {
      throw new Error(`Plain-import lowering received non-declaration stage entry in ${entry.targetFile}`);
    }
    const nextStatements = buildPlainImportOwnerStatements(stageEntry.statement, stageEntry.fragment);
    const boundaryUnit = atomicBoundaryIndex.get(ownerEntryBoundaryKey(stageEntry));
    annotateAtomicBoundary(nextStatements, boundaryUnit, previousAtomicBoundaryUnitId);
    previousAtomicBoundaryUnitId = updatePreviousAtomicBoundaryUnitId(previousAtomicBoundaryUnitId, boundaryUnit);
    body.push(...nextStatements);
  }
  body.push(
    t.exportNamedDeclaration(
      null,
      entry.exportBindings.map((binding) =>
        t.exportSpecifier(t.identifier(binding.local), exportNameNode(binding.exported))
      )
    )
  );
  upgradeSingleAssignmentLetsToConst(body);
  if (bodyStartedAt) {
    addDurationMs(phaseDurationsMs, "body", durationMsSince(bodyStartedAt));
  }
  const rewriteStartedAt = phaseDurationsMs ? process.hrtime.bigint() : null;
  if (needsRuntimeSourceRewrite) {
    rewriteStatementsForTarget(body, entry.targetFile);
  }
  if (rewriteStartedAt) {
    addDurationMs(phaseDurationsMs, "rewrite", durationMsSince(rewriteStartedAt));
  }
  const astStartedAt = phaseDurationsMs ? process.hrtime.bigint() : null;
  const ast = t.file(t.program(body));
  if (astStartedAt) {
    addDurationMs(phaseDurationsMs, "ast", durationMsSince(astStartedAt));
  }
  const renameStartedAt = phaseDurationsMs ? process.hrtime.bigint() : null;
  applyFinalBindingRenamesToGeneratedFile(ast, entryBindingRenames, {
    context: `${entry.targetFile} selected-module lowering`,
  });
  if (renameStartedAt) {
    addDurationMs(phaseDurationsMs, "rename", durationMsSince(renameStartedAt));
  }
  return {
    ast,
    headerLines: buildSelectedModuleLoweringHeaderLines(entry.ownerIds),
  };
}

function buildPlainImportOwnerStatements(statement, fragment = null) {
  if (t.isFunctionDeclaration(statement) || t.isClassDeclaration(statement)) {
    return [statement];
  }
  if (t.isVariableDeclaration(statement)) {
    if (!fragment) {
      return [statement];
    }
    const declarations = fragment.declaratorIndices.map((index) => statement.declarations[index]).filter(Boolean);
    return [
      t.variableDeclaration(
        statement.kind,
        declarations.map((declaration) => t.cloneNode(declaration, true))
      ),
    ];
  }
  throw new Error(`Plain-import lowering does not support ${statement?.type}`);
}

function naturalizedEntryBindingNameSet(entry) {
  return new Set((entry.naturalizedDeclarationEntries ?? []).flatMap(selectedEntryBindingNames));
}

function buildNaturalizedDeclarationStatements(entry) {
  const statements = [];
  const atomicBoundaryIndex = buildAtomicBoundaryIndex(entry.atomicBoundaryUnits ?? []);
  let previousAtomicBoundaryUnitId = null;
  for (const stageEntry of entry.naturalizedDeclarationEntries ?? []) {
    const nextStatements = [naturalizedDeclarationStatement(stageEntry)];
    const boundaryUnit = atomicBoundaryIndex.get(ownerEntryBoundaryKey(stageEntry));
    annotateAtomicBoundary(nextStatements, boundaryUnit, previousAtomicBoundaryUnitId);
    previousAtomicBoundaryUnitId = updatePreviousAtomicBoundaryUnitId(previousAtomicBoundaryUnitId, boundaryUnit);
    statements.push(...nextStatements);
  }
  return statements;
}

function naturalizedDeclarationStatement(stageEntry) {
  if (t.isFunctionDeclaration(stageEntry.statement) || t.isClassDeclaration(stageEntry.statement)) {
    return t.cloneNode(stageEntry.statement, true);
  }
  const shape = declarationShapedVariableEntry(stageEntry);
  if (!shape) {
    throw new Error(`Expected declaration-shaped variable entry for ${stageEntry.owner.id}`);
  }
  const expressionName = shape.expression.id?.name ?? shape.bindingName;
  if (expressionName !== shape.bindingName) {
    return t.variableDeclaration(shape.statement.kind, [t.cloneNode(shape.declaration, true)]);
  }
  if (shape.kind === "function") {
    return t.functionDeclaration(
      t.identifier(shape.bindingName),
      shape.expression.params.map((param) => t.cloneNode(param, true)),
      t.cloneNode(shape.expression.body, true),
      shape.expression.generator,
      shape.expression.async
    );
  }
  return t.classDeclaration(
    t.identifier(shape.bindingName),
    shape.expression.superClass ? t.cloneNode(shape.expression.superClass, true) : null,
    t.cloneNode(shape.expression.body, true),
    shape.expression.decorators?.map((decorator) => t.cloneNode(decorator, true)) ?? []
  );
}

function buildInitStatements(
  stageEntries,
  localRenameMap,
  atomicBoundaryUnits = [],
  { naturalizedDeclarationKeys = new Set() } = {}
) {
  const statements = [];
  const atomicBoundaryIndex = buildAtomicBoundaryIndex(atomicBoundaryUnits);
  let previousAtomicBoundaryUnitId = null;
  for (const entry of stageEntries) {
    if (entry.kind !== "declaration") {
      continue;
    }
    if (naturalizedDeclarationKeys.has(ownerEntryBoundaryKey(entry))) {
      continue;
    }
    const { owner, statement } = entry;
    if (owner.type !== "FunctionDeclaration") {
      continue;
    }
    const nextStatements = [functionDeclarationAssignmentStatement(statement, localRenameMap)];
    const boundaryUnit = atomicBoundaryIndex.get(ownerEntryBoundaryKey(entry));
    annotateAtomicBoundary(nextStatements, boundaryUnit, previousAtomicBoundaryUnitId);
    previousAtomicBoundaryUnitId = updatePreviousAtomicBoundaryUnitId(previousAtomicBoundaryUnitId, boundaryUnit);
    statements.push(...nextStatements);
  }
  for (const entry of stageEntries) {
    if (entry.kind === "side_effect") {
      const nextStatements = [entry.statement];
      annotateAtomicBoundary(
        nextStatements,
        atomicBoundaryIndex.get(entry.sideEffect.id),
        previousAtomicBoundaryUnitId
      );
      previousAtomicBoundaryUnitId = updatePreviousAtomicBoundaryUnitId(
        previousAtomicBoundaryUnitId,
        atomicBoundaryIndex.get(entry.sideEffect.id)
      );
      statements.push(...nextStatements);
      continue;
    }
    const { owner, statement } = entry;
    if (naturalizedDeclarationKeys.has(ownerEntryBoundaryKey(entry))) {
      continue;
    }
    if (owner.type === "FunctionDeclaration") {
      continue;
    }
    const nextStatements = buildOwnerInitStatements(owner, statement, localRenameMap, entry.fragment);
    const boundaryUnit = atomicBoundaryIndex.get(ownerEntryBoundaryKey(entry));
    annotateAtomicBoundary(nextStatements, boundaryUnit, previousAtomicBoundaryUnitId);
    previousAtomicBoundaryUnitId = updatePreviousAtomicBoundaryUnitId(previousAtomicBoundaryUnitId, boundaryUnit);
    statements.push(...nextStatements);
  }
  return statements;
}

function entryNeedsRuntimeSourceRewrite(entry) {
  return (
    entry.ownerEntries.some((ownerEntry) => ownerEntry.owner.effects.containsRuntimeSourceRebase) ||
    entry.attachedEntries.some((attachedEntry) => attachedEntry.sideEffect.effects.containsRuntimeSourceRebase)
  );
}

function buildOwnerInitStatements(owner, statement, localRenameMap, fragment = null) {
  if (
    owner.currentExtractorLowering === "snapshot_variable_declaration" &&
    !supportsDirectFragmentVariableLowering(fragment)
  ) {
    return buildSnapshotVariableDeclarationStatements(owner, statement);
  }
  if (t.isClassDeclaration(statement)) {
    return [classDeclarationAssignmentStatement(statement, localRenameMap)];
  }
  if (t.isVariableDeclaration(statement)) {
    const declarations = fragment
      ? fragment.declaratorIndices.map((index) => statement.declarations[index]).filter(Boolean)
      : statement.declarations;
    return declarations.map((declaration) => variableDeclaratorAssignmentStatement(declaration));
  }
  throw new Error(`Unsupported extracted owner statement type ${statement?.type}`);
}

function supportsDirectFragmentVariableLowering(fragment) {
  return (
    fragment?.kind === "variable_declarator" &&
    Array.isArray(fragment.declaratorIndices) &&
    fragment.declaratorIndices.length === 1
  );
}

function buildEntryBindingRenames(entry) {
  const explicitRenameBySourceName = new Map();
  for (const placement of entry.bindingPlacements ?? []) {
    const localName = preferredLocalBindingName(placement.sourceName, placement.name);
    if (localName === placement.sourceName) {
      continue;
    }
    explicitRenameBySourceName.set(placement.sourceName, {
      from: placement.sourceName,
      source: "logical_member",
      to: localName,
    });
  }
  for (const importRecord of entry.usedExtractedImports ?? []) {
    for (const specifier of importRecord.specifiers) {
      const localName = preferredLocalBindingName(specifier.local, specifier.imported);
      if (localName === specifier.local || explicitRenameBySourceName.has(specifier.local)) {
        continue;
      }
      explicitRenameBySourceName.set(specifier.local, {
        from: specifier.local,
        source: "propagated_dependency",
        to: localName,
      });
    }
  }
  return [...explicitRenameBySourceName.values()].sort((left, right) => left.from.localeCompare(right.from));
}

function buildRuntimeBindingRenames(entries) {
  const renameBySourceName = new Map();
  for (const entry of entries) {
    for (const binding of entry.exportBindings ?? []) {
      const localName = preferredLocalBindingName(binding.local, binding.exported);
      if (localName === binding.local) {
        continue;
      }
      const existing = renameBySourceName.get(binding.local);
      if (existing && existing.to !== localName) {
        throw new Error(
          `Runtime lowering has conflicting final names for ${binding.local}: ${existing.to} vs ${localName}`
        );
      }
      renameBySourceName.set(binding.local, {
        from: binding.local,
        source: "runtime_import",
        to: localName,
      });
    }
  }
  return [...renameBySourceName.values()].sort((left, right) => left.from.localeCompare(right.from));
}

function buildBindingRenameMap(renameSpecs) {
  return new Map(renameSpecs.map((renameSpec) => [renameSpec.from, renameSpec.to]));
}

function preferredLocalBindingName(sourceName, requestedName) {
  return typeof requestedName === "string" && t.isValidIdentifier(requestedName) ? requestedName : sourceName;
}

function renamedLocalIdentifierName(name, localRenameMap) {
  return localRenameMap.get(name) ?? name;
}

function applyFinalBindingRenamesToGeneratedFile(ast, renameSpecs, { context }) {
  const mayHaveReadableRenames = generatedFileMayHaveReadableRenameCandidate(ast);
  if (!Array.isArray(renameSpecs) || renameSpecs.length === 0) {
    if (mayHaveReadableRenames) {
      applyReadableObjectPatternRenamesInGeneratedFile(ast);
    }
    return;
  }
  let collectReadableRenames = false;
  let readableCandidatesByScope = null;
  traverse(ast, {
    Program: {
      enter(path) {
        validateRenameSpecsAgainstProgramScope(path, renameSpecs, context);
        const bindingBySourceName = new Map();
        const renameBySourceName = new Map();
        for (const renameSpec of renameSpecs) {
          const binding = path.scope.getOwnBinding(renameSpec.from);
          if (!binding) {
            continue;
          }
          bindingBySourceName.set(renameSpec.from, binding);
          renameBySourceName.set(renameSpec.from, renameSpec);
        }
        if (renameBySourceName.size === 0) {
          path.skip();
          return;
        }
        const renameByBinding = new Map();
        for (const [sourceName, renameSpec] of renameBySourceName) {
          const binding = bindingBySourceName.get(sourceName);
          if (binding) {
            renameByBinding.set(binding, renameSpec.to);
          }
        }
        applyBindingLocalRenamesInProgram(path, renameByBinding);
        if (mayHaveReadableRenames) {
          collectReadableRenames = true;
          readableCandidatesByScope = new Map();
        } else {
          path.skip();
        }
      },
      exit() {
        if (readableCandidatesByScope) {
          applyReadableObjectPatternRenamesFromCandidates(readableCandidatesByScope);
        }
      },
    },
    Function(functionPath) {
      if (!collectReadableRenames) {
        return;
      }
      for (const param of functionPath.node.params ?? []) {
        collectReadableObjectPatternScopeCandidates(functionPath, param, readableCandidatesByScope);
      }
    },
    VariableDeclarator(variableDeclaratorPath) {
      if (!collectReadableRenames) {
        return;
      }
      collectReadableObjectPatternScopeCandidates(
        variableDeclaratorPath,
        variableDeclaratorPath.node.id,
        readableCandidatesByScope
      );
    },
    AssignmentExpression(assignmentPath) {
      if (!collectReadableRenames) {
        return;
      }
      collectReadableObjectPatternScopeCandidates(assignmentPath, assignmentPath.node.left, readableCandidatesByScope);
    },
    ClassMethod(classMethodPath) {
      if (!collectReadableRenames) {
        return;
      }
      collectReadableConstructorParamScopeCandidates(classMethodPath, readableCandidatesByScope);
    },
    ObjectExpression(objectExpressionPath) {
      if (!collectReadableRenames) {
        return;
      }
      collectReadableObjectExpressionScopeCandidates(objectExpressionPath, readableCandidatesByScope);
    },
  });
}

function generatedFileMayHaveReadableRenameCandidate(ast) {
  const stack = [ast];
  while (stack.length > 0) {
    const node = stack.pop();
    if (!node || typeof node.type !== "string") {
      continue;
    }
    if (t.isObjectPattern(node) && objectPatternMayHaveReadableRenameCandidate(node)) {
      return true;
    }
    if (t.isObjectExpression(node) && objectExpressionMayHaveReadableRenameCandidate(node)) {
      return true;
    }
    if (t.isClassMethod(node) && constructorMayHaveReadableParamCandidate(node)) {
      return true;
    }
    for (const key of t.VISITOR_KEYS[node.type] ?? []) {
      const child = node[key];
      if (Array.isArray(child)) {
        for (let index = child.length - 1; index >= 0; index--) {
          stack.push(child[index]);
        }
      } else if (child) {
        stack.push(child);
      }
    }
  }
  return false;
}

function objectExpressionMayHaveReadableRenameCandidate(node) {
  for (const property of node.properties ?? []) {
    if (!t.isObjectProperty(property) || !t.isIdentifier(property.value)) {
      continue;
    }
    const desiredName = readableObjectPropertyBindingName(property);
    if (desiredName && readableRenameCandidateNames(property.value.name, desiredName)) {
      return true;
    }
  }
  return false;
}

function objectPatternMayHaveReadableRenameCandidate(pattern) {
  if (!pattern) {
    return false;
  }
  if (t.isAssignmentPattern(pattern)) {
    return objectPatternMayHaveReadableRenameCandidate(pattern.left);
  }
  if (t.isRestElement(pattern)) {
    return objectPatternMayHaveReadableRenameCandidate(pattern.argument);
  }
  if (t.isArrayPattern(pattern)) {
    return pattern.elements.some((element) => objectPatternMayHaveReadableRenameCandidate(element));
  }
  if (!t.isObjectPattern(pattern)) {
    return false;
  }
  for (const property of pattern.properties ?? []) {
    if (t.isRestElement(property)) {
      if (objectPatternMayHaveReadableRenameCandidate(property.argument)) {
        return true;
      }
      continue;
    }
    if (!t.isObjectProperty(property)) {
      continue;
    }
    const desiredName = readableObjectPropertyBindingName(property);
    if (propertyValueMayHaveReadableRenameCandidate(property.value, desiredName)) {
      return true;
    }
  }
  return false;
}

function propertyValueMayHaveReadableRenameCandidate(value, desiredName) {
  if (t.isIdentifier(value)) {
    return Boolean(desiredName && readableRenameCandidateNames(value.name, desiredName));
  }
  if (t.isAssignmentPattern(value)) {
    return propertyValueMayHaveReadableRenameCandidate(value.left, desiredName);
  }
  return objectPatternMayHaveReadableRenameCandidate(value);
}

function constructorMayHaveReadableParamCandidate(node) {
  if (node.kind !== "constructor") {
    return false;
  }
  const paramNames = new Set(
    (node.params ?? [])
      .filter((param) => t.isIdentifier(param) && isScrambledIdentifier(param.name))
      .map((param) => param.name)
  );
  if (paramNames.size === 0) {
    return false;
  }
  return constructorBodyMayHaveReadableParamAssignment(node.body, paramNames);
}

function constructorBodyMayHaveReadableParamAssignment(body, paramNames) {
  const stack = [body];
  while (stack.length > 0) {
    const node = stack.pop();
    if (!node || typeof node.type !== "string") {
      continue;
    }
    if (node !== body && (t.isFunction(node) || t.isClass(node))) {
      continue;
    }
    if (t.isAssignmentExpression(node) && node.operator === "=" && t.isIdentifier(node.right)) {
      const desiredName = readableThisPropertyAssignmentName(node.left);
      if (
        desiredName &&
        paramNames.has(node.right.name) &&
        readableRenameCandidateNames(node.right.name, desiredName)
      ) {
        return true;
      }
    }
    for (const key of t.VISITOR_KEYS[node.type] ?? []) {
      const child = node[key];
      if (Array.isArray(child)) {
        for (let index = child.length - 1; index >= 0; index--) {
          stack.push(child[index]);
        }
      } else if (child) {
        stack.push(child);
      }
    }
  }
  return false;
}

function readableRenameCandidateNames(sourceName, targetName) {
  return sourceName !== targetName && isScrambledIdentifier(sourceName);
}

function applyReadableObjectPatternRenamesInGeneratedFile(ast) {
  traverse(ast, {
    Program(path) {
      applyReadableObjectPatternRenamesInProgram(path);
    },
  });
}

function applyReadableObjectPatternRenamesInProgram(programPath) {
  const renameGroups = buildReadableObjectPatternRenameGroups(programPath);
  const renameByBinding = new Map();
  for (const { scopePath, renameSpecs } of renameGroups) {
    for (const renameSpec of renameSpecs) {
      const binding = scopePath.scope.getOwnBinding(renameSpec.from);
      if (!binding) {
        continue;
      }
      renameByBinding.set(binding, renameSpec.to);
    }
  }
  applyBindingLocalRenamesInProgram(programPath, renameByBinding);
}

function applyBindingLocalRenamesInProgram(_programPath, renameByBinding) {
  if (renameByBinding.size === 0) {
    return;
  }
  const bindingIdentifierPathByNode = buildBindingIdentifierPathIndex(renameByBinding);
  const objectPropertyContainersToRefresh = new Set();
  for (const [binding, renameTarget] of renameByBinding) {
    const sourceName = binding.identifier?.name;
    if (!sourceName || sourceName === renameTarget) {
      continue;
    }
    renameBindingIdentifier(
      binding,
      sourceName,
      renameTarget,
      bindingIdentifierPathByNode,
      objectPropertyContainersToRefresh
    );
    for (const referencePath of binding.referencePaths ?? []) {
      renameIdentifierPath(referencePath, sourceName, renameTarget, objectPropertyContainersToRefresh);
    }
    for (const violationPath of binding.constantViolations ?? []) {
      renameBindingIdentifiersInPath(
        violationPath,
        binding,
        sourceName,
        renameTarget,
        objectPropertyContainersToRefresh
      );
    }
  }
  for (const containerPath of objectPropertyContainersToRefresh) {
    for (const siblingPath of containerPath.get("properties")) {
      if (siblingPath.isObjectProperty()) {
        refreshObjectPropertyShorthand(siblingPath);
      }
    }
  }
}

function buildBindingIdentifierPathIndex(renameByBinding) {
  const bindingsByPath = new Map();
  for (const binding of renameByBinding.keys()) {
    if (!binding.path || binding.path.isIdentifier?.()) {
      continue;
    }
    const bindings = bindingsByPath.get(binding.path) ?? [];
    bindings.push(binding);
    bindingsByPath.set(binding.path, bindings);
  }
  const identifierPathByNode = new Map();
  for (const [declarationPath, bindings] of bindingsByPath) {
    const bindingIdentifierNodes = new Set(bindings.map((binding) => binding.identifier).filter(Boolean));
    let foundCount = 0;
    declarationPath.traverse?.({
      Identifier(identifierPath) {
        if (!bindingIdentifierNodes.has(identifierPath.node)) {
          return;
        }
        identifierPathByNode.set(identifierPath.node, identifierPath);
        foundCount += 1;
        if (foundCount >= bindingIdentifierNodes.size) {
          identifierPath.stop();
        }
      },
    });
  }
  return identifierPathByNode;
}

function renameBindingIdentifier(
  binding,
  sourceName,
  renameTarget,
  bindingIdentifierPathByNode,
  objectPropertyContainersToRefresh
) {
  const bindingIdentifierPath = binding.path?.isIdentifier?.()
    ? binding.path
    : bindingIdentifierPathByNode.get(binding.identifier);
  if (bindingIdentifierPath) {
    renameIdentifierPath(bindingIdentifierPath, sourceName, renameTarget, objectPropertyContainersToRefresh);
    return;
  }
  if (binding.identifier?.name === sourceName) {
    binding.identifier.name = renameTarget;
  }
}

function renameBindingIdentifiersInPath(path, binding, sourceName, renameTarget, objectPropertyContainersToRefresh) {
  if (!path) {
    return;
  }
  if (path.isIdentifier?.()) {
    if (path.scope.getBinding(sourceName) === binding) {
      renameIdentifierPath(path, sourceName, renameTarget, objectPropertyContainersToRefresh);
    }
    return;
  }
  path.traverse({
    Identifier(identifierPath) {
      if (identifierPath.node.name !== sourceName) {
        return;
      }
      if (identifierPath.scope.getBinding(sourceName) !== binding) {
        return;
      }
      renameIdentifierPath(identifierPath, sourceName, renameTarget, objectPropertyContainersToRefresh);
    },
  });
}

function renameIdentifierPath(identifierPath, sourceName, renameTarget, objectPropertyContainersToRefresh) {
  if (identifierPath.node.name !== sourceName || !shouldRenameIdentifierPath(identifierPath)) {
    return;
  }
  identifierPath.node.name = renameTarget;
  collectParentObjectPropertyContainer(identifierPath, objectPropertyContainersToRefresh);
}

function collectParentObjectPropertyContainer(identifierPath, objectPropertyContainersToRefresh) {
  const propertyPath = identifierPath.parentPath;
  if (!propertyPath?.isObjectProperty?.({ value: identifierPath.node })) {
    return;
  }
  const containerPath = propertyPath.parentPath;
  if (!containerPath.isObjectPattern() && !containerPath.isObjectExpression()) {
    return;
  }
  objectPropertyContainersToRefresh.add(containerPath);
}

function refreshObjectPropertyShorthand(propertyPath) {
  if (!propertyPath.parentPath.isObjectPattern() && !propertyPath.parentPath.isObjectExpression()) {
    return;
  }
  if (
    propertyPath.node.computed ||
    !t.isIdentifier(propertyPath.node.key) ||
    !t.isIdentifier(propertyPath.node.value)
  ) {
    propertyPath.node.shorthand = false;
    return;
  }
  propertyPath.node.shorthand = propertyPath.node.key.name === propertyPath.node.value.name;
}

function buildReadableObjectPatternRenameGroups(programPath) {
  const candidatesByScope = new Map();
  programPath.traverse({
    Function(functionPath) {
      for (const param of functionPath.node.params ?? []) {
        collectReadableObjectPatternScopeCandidates(functionPath, param, candidatesByScope);
      }
    },
    VariableDeclarator(variableDeclaratorPath) {
      collectReadableObjectPatternScopeCandidates(
        variableDeclaratorPath,
        variableDeclaratorPath.node.id,
        candidatesByScope
      );
    },
    AssignmentExpression(assignmentPath) {
      collectReadableObjectPatternScopeCandidates(assignmentPath, assignmentPath.node.left, candidatesByScope);
    },
    ClassMethod(classMethodPath) {
      collectReadableConstructorParamScopeCandidates(classMethodPath, candidatesByScope);
    },
    ObjectExpression(objectExpressionPath) {
      collectReadableObjectExpressionScopeCandidates(objectExpressionPath, candidatesByScope);
    },
  });

  return buildReadableObjectPatternRenameGroupsFromCandidates(candidatesByScope);
}

function applyReadableObjectPatternRenamesFromCandidates(candidatesByScope) {
  const renameGroups = buildReadableObjectPatternRenameGroupsFromCandidates(candidatesByScope);
  const renameByBinding = new Map();
  for (const { scopePath, renameSpecs } of renameGroups) {
    for (const renameSpec of renameSpecs) {
      const binding = scopePath.scope.getOwnBinding(renameSpec.from);
      if (!binding) {
        continue;
      }
      renameByBinding.set(binding, renameSpec.to);
    }
  }
  applyBindingLocalRenamesInProgram(null, renameByBinding);
}

function buildReadableObjectPatternRenameGroupsFromCandidates(candidatesByScope) {
  const renameGroups = [];
  for (const { scopePath, candidates } of candidatesByScope.values()) {
    const candidateBySourceName = new Map();
    const targetCounts = new Map();
    for (const candidate of candidates) {
      const existing = candidateBySourceName.get(candidate.from);
      if (existing) {
        if (existing.to !== candidate.to) {
          continue;
        }
        continue;
      }
      candidateBySourceName.set(candidate.from, candidate);
      targetCounts.set(candidate.to, (targetCounts.get(candidate.to) ?? 0) + 1);
    }
    const externallyCapturedTargetNames = collectExternallyCapturedTargetNames(
      scopePath,
      new Set([...candidateBySourceName.values()].map((candidate) => candidate.to))
    );
    const renameSpecs = [...candidateBySourceName.values()]
      .filter((candidate) =>
        isSafeReadableObjectPatternRename(scopePath, candidate, targetCounts, externallyCapturedTargetNames)
      )
      .map((candidate) => ({
        from: candidate.from,
        to: candidate.to,
      }));
    if (renameSpecs.length > 0) {
      renameGroups.push({
        scopePath,
        renameSpecs,
      });
    }
  }
  return renameGroups;
}

function collectReadableObjectPatternScopeCandidates(scopePath, pattern, candidatesByScope) {
  const rawCandidates = [];
  collectReadableObjectPatternRenameCandidates(pattern, rawCandidates);
  for (const candidate of rawCandidates) {
    const binding = scopePath.scope.getBinding(candidate.from);
    if (!binding) {
      continue;
    }
    const bindingScopePath = binding.scope.path;
    const bindingScopeCandidates = candidatesByScope.get(bindingScopePath) ?? {
      scopePath: bindingScopePath,
      candidates: [],
    };
    bindingScopeCandidates.candidates.push({
      ...candidate,
      binding,
    });
    candidatesByScope.set(bindingScopePath, bindingScopeCandidates);
  }
}

function collectReadableObjectExpressionScopeCandidates(scopePath, candidatesByScope) {
  const rawCandidates = [];
  collectReadableObjectExpressionRenameCandidates(scopePath.node, rawCandidates);
  for (const candidate of rawCandidates) {
    const binding = scopePath.scope.getBinding(candidate.from);
    if (!binding) {
      continue;
    }
    const bindingScopePath = binding.scope.path;
    const bindingScopeCandidates = candidatesByScope.get(bindingScopePath) ?? {
      scopePath: bindingScopePath,
      candidates: [],
    };
    bindingScopeCandidates.candidates.push({
      ...candidate,
      binding,
    });
    candidatesByScope.set(bindingScopePath, bindingScopeCandidates);
  }
}

function collectReadableConstructorParamScopeCandidates(classMethodPath, candidatesByScope) {
  if (classMethodPath.node.kind !== "constructor") {
    return;
  }
  const paramNames = new Set();
  for (const param of classMethodPath.node.params ?? []) {
    if (t.isIdentifier(param)) {
      paramNames.add(param.name);
    }
  }
  if (paramNames.size === 0) {
    return;
  }

  const candidateBySourceName = new Map();
  const ambiguousSourceNames = new Set();
  classMethodPath.get("body").traverse({
    Function(functionPath) {
      functionPath.skip();
    },
    Class(classPath) {
      classPath.skip();
    },
    AssignmentExpression(assignmentPath) {
      const candidate = readableConstructorParamAssignmentCandidate(assignmentPath.node, paramNames);
      if (!candidate) {
        return;
      }
      const existing = candidateBySourceName.get(candidate.from);
      if (existing && existing.to !== candidate.to) {
        ambiguousSourceNames.add(candidate.from);
        return;
      }
      candidateBySourceName.set(candidate.from, candidate);
    },
  });

  for (const candidate of candidateBySourceName.values()) {
    if (ambiguousSourceNames.has(candidate.from)) {
      continue;
    }
    const binding = classMethodPath.scope.getOwnBinding(candidate.from);
    if (!binding || binding.kind !== "param") {
      continue;
    }
    const bindingScopePath = binding.scope.path;
    const bindingScopeCandidates = candidatesByScope.get(bindingScopePath) ?? {
      scopePath: bindingScopePath,
      candidates: [],
    };
    bindingScopeCandidates.candidates.push({
      ...candidate,
      binding,
    });
    candidatesByScope.set(bindingScopePath, bindingScopeCandidates);
  }
}

function readableConstructorParamAssignmentCandidate(node, paramNames) {
  if (!t.isAssignmentExpression(node) || node.operator !== "=") {
    return null;
  }
  if (!t.isIdentifier(node.right) || !paramNames.has(node.right.name)) {
    return null;
  }
  const targetName = readableThisPropertyAssignmentName(node.left);
  if (!targetName) {
    return null;
  }
  return {
    from: node.right.name,
    to: targetName,
  };
}

function readableThisPropertyAssignmentName(node) {
  if (!t.isMemberExpression(node) || !t.isThisExpression(node.object)) {
    return null;
  }
  if (node.computed) {
    return t.isStringLiteral(node.property) && t.isValidIdentifier(node.property.value) ? node.property.value : null;
  }
  return t.isIdentifier(node.property) ? node.property.name : null;
}

function isSafeReadableObjectPatternRename(scopePath, candidate, targetCounts, externallyCapturedTargetNames) {
  if (candidate.from === candidate.to || !isScrambledIdentifier(candidate.from)) {
    return false;
  }
  const binding = candidate.binding ?? scopePath.scope.getOwnBinding(candidate.from);
  if (!binding || binding.scope !== scopePath.scope || !["param", "const", "let", "var"].includes(binding.kind)) {
    return false;
  }
  if ((targetCounts.get(candidate.to) ?? 0) > 1) {
    return false;
  }
  const existingBinding = scopePath.scope.getOwnBinding(candidate.to);
  if (existingBinding && existingBinding !== binding) {
    return false;
  }
  if (externallyCapturedTargetNames.has(candidate.to)) {
    return false;
  }
  return !bindingWouldBeShadowedAfterReadableRename(binding, candidate.to);
}

function collectExternallyCapturedTargetNames(scopePath, targetNames) {
  const externallyCapturedTargetNames = new Set();
  if (targetNames.size === 0) {
    return externallyCapturedTargetNames;
  }
  for (const targetName of targetNames) {
    const binding = scopePath.scope.getBinding(targetName);
    if (!binding || binding.scope === scopePath.scope) {
      continue;
    }
    if (binding.referencePaths?.some((referencePath) => pathIsWithinNode(referencePath, scopePath.node))) {
      externallyCapturedTargetNames.add(targetName);
    }
  }
  return externallyCapturedTargetNames;
}

function pathIsWithinNode(path, ancestorNode) {
  if (!path) {
    return false;
  }
  if (path.node === ancestorNode) {
    return true;
  }
  return Boolean(path.findParent((parentPath) => parentPath.node === ancestorNode));
}

function bindingWouldBeShadowedAfterReadableRename(binding, targetName) {
  if (binding.identifier?.name === targetName) {
    return false;
  }
  for (const referencePath of binding.referencePaths ?? []) {
    if (pathHasDescendantShadowBinding(referencePath, binding.scope, targetName)) {
      return true;
    }
  }
  for (const violationPath of binding.constantViolations ?? []) {
    if (pathHasDescendantShadowBinding(violationPath, binding.scope, targetName)) {
      return true;
    }
  }
  return false;
}

function pathHasDescendantShadowBinding(path, ownerScope, targetName) {
  let currentScope = path.scope;
  while (currentScope && currentScope !== ownerScope) {
    if (currentScope.hasOwnBinding(targetName)) {
      return true;
    }
    currentScope = currentScope.parent;
  }
  return false;
}

function collectReadableObjectPatternRenameCandidates(pattern, candidates) {
  if (!pattern) {
    return;
  }
  if (t.isAssignmentPattern(pattern)) {
    collectReadableObjectPatternRenameCandidates(pattern.left, candidates);
    return;
  }
  if (t.isRestElement(pattern)) {
    collectReadableObjectPatternRenameCandidates(pattern.argument, candidates);
    return;
  }
  if (t.isArrayPattern(pattern)) {
    for (const element of pattern.elements) {
      collectReadableObjectPatternRenameCandidates(element, candidates);
    }
    return;
  }
  if (!t.isObjectPattern(pattern)) {
    return;
  }
  for (const property of pattern.properties) {
    if (t.isRestElement(property)) {
      collectReadableObjectPatternRenameCandidates(property.argument, candidates);
      continue;
    }
    if (!t.isObjectProperty(property)) {
      continue;
    }
    const desiredName = readableObjectPropertyBindingName(property);
    collectReadablePropertyValueRenameCandidates(property.value, desiredName, candidates);
  }
}

function collectReadablePropertyValueRenameCandidates(value, desiredName, candidates) {
  if (t.isIdentifier(value)) {
    if (desiredName) {
      candidates.push({
        from: value.name,
        to: desiredName,
      });
    }
    return;
  }
  if (t.isAssignmentPattern(value)) {
    collectReadablePropertyValueRenameCandidates(value.left, desiredName, candidates);
    return;
  }
  collectReadableObjectPatternRenameCandidates(value, candidates);
}

function collectReadableObjectExpressionRenameCandidates(node, candidates) {
  if (!t.isObjectExpression(node)) {
    return;
  }
  for (const property of node.properties) {
    if (!t.isObjectProperty(property)) {
      continue;
    }
    const desiredName = readableObjectPropertyBindingName(property);
    if (t.isIdentifier(property.value) && desiredName) {
      candidates.push({
        from: property.value.name,
        to: desiredName,
      });
    }
  }
}

function readableObjectPropertyBindingName(property) {
  if (property.computed) {
    return null;
  }
  if (t.isIdentifier(property.key)) {
    return property.key.name;
  }
  if (t.isStringLiteral(property.key) && t.isValidIdentifier(property.key.value)) {
    return property.key.value;
  }
  return null;
}

function shouldRenameIdentifierPath(identifierPath) {
  if (identifierPath.isReferencedIdentifier() || identifierPath.isBindingIdentifier()) {
    return true;
  }
  if (identifierPath.parentPath.isExportSpecifier() && identifierPath.key === "local") {
    return true;
  }
  if (identifierPath.parentPath.isAssignmentExpression({ left: identifierPath.node })) {
    return true;
  }
  if (identifierPath.parentPath.isUpdateExpression({ argument: identifierPath.node })) {
    return true;
  }
  if (identifierPath.parentPath.isUnaryExpression({ argument: identifierPath.node, operator: "delete" })) {
    return true;
  }
  if (identifierPath.parentPath.isObjectProperty({ value: identifierPath.node }) && identifierPath.parent.shorthand) {
    return true;
  }
  return false;
}

function validateRenameSpecsAgainstProgramScope(programPath, renameSpecs, context) {
  const renameBySourceName = new Map(renameSpecs.map((renameSpec) => [renameSpec.from, renameSpec]));
  const duplicateFinalNames = findDuplicateStrings(renameSpecs.map((renameSpec) => renameSpec.to));
  if (duplicateFinalNames.length > 0) {
    throw new Error(`${context} assigns duplicate final local names: ${duplicateFinalNames.join(", ")}`);
  }
  for (const renameSpec of renameSpecs) {
    if (renameSpec.from === renameSpec.to) {
      continue;
    }
    const fromBinding = programPath.scope.getOwnBinding(renameSpec.from);
    if (!fromBinding) {
      continue;
    }
    const toBinding = programPath.scope.getOwnBinding(renameSpec.to);
    if (!toBinding) {
      continue;
    }
    if (renameBySourceName.has(renameSpec.to)) {
      throw new Error(
        `${context} propagated final name collision: ${renameSpec.from} -> ${renameSpec.to} would shadow another renamed binding`
      );
    }
    if (toBinding !== fromBinding) {
      throw new Error(
        `${context} final local name ${renameSpec.to} for ${renameSpec.from} conflicts with existing top-level binding`
      );
    }
  }
}

function buildAtomicBoundaryIndex(atomicBoundaryUnits) {
  const index = new Map();
  for (const unit of atomicBoundaryUnits) {
    for (const fragment of unit.ownerFragments ?? []) {
      index.set(fragment.id, unit);
    }
    for (const ownerId of unit.ownerIds ?? []) {
      if (!index.has(ownerId)) {
        index.set(ownerId, unit);
      }
    }
    for (const attachedItemId of unit.attachedItemIds ?? []) {
      index.set(attachedItemId, unit);
    }
  }
  return index;
}

function ownerEntryBoundaryKey(entry) {
  return entry.fragment?.id ?? entry.owner.id;
}

function annotateAtomicBoundary(statements, boundaryUnit, previousAtomicBoundaryUnitId) {
  if (!boundaryUnit || statements.length === 0 || boundaryUnit.id === previousAtomicBoundaryUnitId) {
    return;
  }
  const fragmentComment =
    Array.isArray(boundaryUnit.ownerFragments) && boundaryUnit.ownerFragments.length > 0
      ? ` fragments=${boundaryUnit.ownerFragments.map((fragment) => fragment.id).join(",")}`
      : "";
  t.addComment(
    statements[0],
    "leading",
    ` ${SELECTED_MODULE_ATOMIC_BOUNDARY_PRAGMA} id=${boundaryUnit.id} members=${(boundaryUnit.memberNames ?? []).join(",")} owners=${(boundaryUnit.ownerIds ?? []).join(",")}${fragmentComment} `
  );
}

function updatePreviousAtomicBoundaryUnitId(previousAtomicBoundaryUnitId, boundaryUnit) {
  return boundaryUnit?.id ?? previousAtomicBoundaryUnitId;
}

function buildSnapshotVariableDeclarationStatements(owner, statement) {
  const declaration = unwrapTopLevelDeclarationNode(statement);
  if (!t.isVariableDeclaration(declaration)) {
    throw new Error(`Expected VariableDeclaration for snapshot lowering of ${owner.id}, got ${statement?.type}`);
  }
  const bindingNames = topLevelDeclarationNames(statement);
  const snapshotId = t.identifier(selectedModuleSnapshotIdentifierName(owner.id));
  const snapshotObject = t.objectExpression(
    bindingNames.map((name) => t.objectProperty(t.identifier(name), t.identifier(name), false, true))
  );
  return [
    t.variableDeclaration("const", [
      t.variableDeclarator(
        snapshotId,
        t.callExpression(
          t.parenthesizedExpression(
            t.arrowFunctionExpression(
              [],
              t.blockStatement([t.cloneNode(declaration, true), t.returnStatement(snapshotObject)])
            )
          ),
          []
        )
      ),
    ]),
    ...bindingNames.map((name) =>
      t.expressionStatement(
        t.assignmentExpression("=", t.identifier(name), t.memberExpression(snapshotId, t.identifier(name)))
      )
    ),
  ];
}

function runtimeInitNamesForEntry(entry) {
  if (entry.plainImportEligible) {
    return [];
  }
  return entry.stageRuns
    .map((stageRun, stageIndex) =>
      stageRunNeedsInitWork(entry, stageRun) ? publicStageInitName(entry, stageIndex) : null
    )
    .filter(Boolean);
}

function runtimeInitNameForRun(run) {
  return publicStageInitName(run.entry, run.stageIndex);
}

function moduleStagesForEntry(entry) {
  return entry.stageRuns
    .map((stageRun, stageIndex) => ({
      initName: publicStageInitName(entry, stageIndex),
      stageEntries: stageRun.stageEntries,
      stageRun,
    }))
    .filter((stage) => stageRunNeedsInitWork(entry, stage.stageRun));
}

function stageRunNeedsInitWork(entry, stageRun) {
  return stageRun.stageEntries.some((stageEntry) => stageEntryNeedsInitWork(entry, stageEntry));
}

function stageEntryNeedsInitWork(entry, stageEntry) {
  if (stageEntry.kind !== "declaration") {
    return true;
  }
  if (isTrivialAliasDeclarationEntry(stageEntry)) {
    return false;
  }
  return !entry.naturalizedDeclarationKeys?.has(ownerEntryBoundaryKey(stageEntry));
}

function isTrivialAliasDeclarationEntry(stageEntry) {
  const declaration = trivialAliasDeclaration(stageEntry);
  return declaration != null;
}

function trivialAliasDeclaration(stageEntry) {
  if (stageEntry.kind !== "declaration") {
    return null;
  }
  const { owner, statement, fragment } = stageEntry;
  if (owner.type !== "VariableDeclaration" || fragment) {
    return null;
  }
  if (!t.isVariableDeclaration(statement) || statement.declarations.length !== 1) {
    return null;
  }
  const declarator = statement.declarations[0];
  if (!t.isIdentifier(declarator.id) || !declarator.init) {
    return null;
  }
  if (!isTrivialAliasInitializer(declarator.init)) {
    return null;
  }
  return t.variableDeclaration("const", [
    t.variableDeclarator(t.cloneNode(declarator.id), t.cloneNode(declarator.init, true)),
  ]);
}

function isTrivialAliasInitializer(node) {
  if (t.isIdentifier(node)) {
    return true;
  }
  if (t.isMemberExpression(node) && !node.computed) {
    return isTrivialAliasInitializer(node.object) && t.isIdentifier(node.property);
  }
  return false;
}

function publicStageInitName(entry, stageIndex) {
  return entry.stageRuns.length === 1 ? entry.initName : stageInitName(entry.initName, stageIndex);
}

function stageInitName(initName, stageIndex) {
  return `${initName}_stage_${stageIndex}`;
}

function functionDeclarationAssignmentStatement(statement, localRenameMap) {
  if (!t.isFunctionDeclaration(statement) || !statement.id) {
    throw new Error(`Expected FunctionDeclaration, got ${statement?.type}`);
  }
  const localName = renamedLocalIdentifierName(statement.id.name, localRenameMap);
  return t.expressionStatement(
    t.assignmentExpression(
      "=",
      t.identifier(localName),
      t.functionExpression(
        t.identifier(localName),
        statement.params,
        statement.body,
        statement.generator,
        statement.async
      )
    )
  );
}

function classDeclarationAssignmentStatement(statement, localRenameMap) {
  if (!t.isClassDeclaration(statement) || !statement.id) {
    throw new Error(`Expected ClassDeclaration, got ${statement?.type}`);
  }
  const localName = renamedLocalIdentifierName(statement.id.name, localRenameMap);
  return t.expressionStatement(
    t.assignmentExpression(
      "=",
      t.identifier(localName),
      t.classExpression(
        t.identifier(localName),
        statement.superClass ?? null,
        statement.body,
        statement.decorators ?? []
      )
    )
  );
}

function variableDeclaratorAssignmentStatement(declaration) {
  const assignment = t.assignmentExpression("=", declaration.id, declaration.init ?? t.identifier("undefined"));
  return t.expressionStatement(t.isIdentifier(declaration.id) ? assignment : t.parenthesizedExpression(assignment));
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
  const blockShadowedRuntimeConstructors = shadowedRuntimeConstructors | collectBlockScopedShadowMask(statements);
  for (const statement of statements) {
    rewriteRuntimeSourcesInNode(statement, rewriteImportSource, blockShadowedRuntimeConstructors);
  }
}

function rewriteSwitchRuntimeSources(node, rewriteImportSource, shadowedRuntimeConstructors) {
  rewriteRuntimeSourcesInNode(node.discriminant, rewriteImportSource, shadowedRuntimeConstructors);
  const switchShadowedRuntimeConstructors = shadowedRuntimeConstructors | collectSwitchScopedShadowMask(node.cases);
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
  const loopShadowedRuntimeConstructors = shadowedRuntimeConstructors | collectLoopScopedShadowMask(node);
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
  const classShadowedRuntimeConstructors = shadowedRuntimeConstructors | runtimeConstructorBindingShadowMask(node.id);
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
    recordShadowMask(
      runtimeConstructorBindingShadowMaskForNodes(node.declarations.map((declaration) => declaration.id))
    );
  }
  visitChildNodes(node, (child) => collectFunctionVarShadowMaskInNode(child, recordShadowMask));
}

function collectBlockScopedShadowMask(statements) {
  let shadowMask = RUNTIME_CONSTRUCTOR_SHADOW_NONE;
  for (const statement of statements) {
    if (t.isVariableDeclaration(statement) && statement.kind !== "var") {
      shadowMask |= runtimeConstructorBindingShadowMaskForNodes(
        statement.declarations.map((declaration) => declaration.id)
      );
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
  if (
    (t.isForInStatement(node) || t.isForOfStatement(node)) &&
    t.isVariableDeclaration(node.left) &&
    node.left.kind !== "var"
  ) {
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
  const rebased = fromDir === "." ? normalizedSource : relativeBetween(fromDir, normalizedSource);
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

function collectSelectedEntryExportNames(ownerEntries) {
  const names = [];
  for (const entry of ownerEntries) {
    const selectedNames = entry.fragment?.memberNames ?? topLevelDeclarationNames(entry.statement);
    for (const name of selectedNames) {
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
  const declaredNames = statement.declarations.flatMap((declaration) => bindingNames(declaration.id));
  const declaredNameSet = new Set(declaredNames);
  const availableNames = new Set();
  for (const declaration of statement.declarations) {
    for (const referencedName of referencedUndeclaredNamesInVariableDeclarator(declaration)) {
      if (!declaredNameSet.has(referencedName)) {
        continue;
      }
      if (!availableNames.has(referencedName)) {
        throw new Error(
          `Extract operation ${operationId} does not support forward/self variable references in ${ownerId}`
        );
      }
    }
    for (const declaredName of bindingNames(declaration.id)) {
      availableNames.add(declaredName);
    }
  }
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

function buildSelectedBindingCoverage(ownerEntries) {
  const coverage = new Map();
  for (const ownerEntry of ownerEntries) {
    if (!coverage.has(ownerEntry.owner.id)) {
      coverage.set(ownerEntry.owner.id, {
        fullOwner: false,
        names: new Set(),
      });
    }
    const ownerCoverage = coverage.get(ownerEntry.owner.id);
    if (!ownerEntry.fragment) {
      ownerCoverage.fullOwner = true;
      ownerCoverage.names = new Set(topLevelDeclarationNames(ownerEntry.statement));
      continue;
    }
    for (const name of ownerEntry.fragment.memberNames ?? []) {
      ownerCoverage.names.add(name);
    }
  }
  return coverage;
}

function currentOperationSelectsBinding(selectedBindingCoverage, ownerId, name) {
  const coverage = selectedBindingCoverage.get(ownerId);
  if (!coverage) {
    return false;
  }
  return coverage.fullOwner || coverage.names.has(name);
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
  if (operation.selector.attachedItemIds !== undefined && !Array.isArray(operation.selector.attachedItemIds)) {
    throw new Error(`Extract operation ${operation.id} selector.attachedItemIds must be an array when present`);
  }
  if (operation.selector.ownerFragments !== undefined && !Array.isArray(operation.selector.ownerFragments)) {
    throw new Error(`Extract operation ${operation.id} selector.ownerFragments must be an array when present`);
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
    if (operation.operation !== "lower_selected_module_region") {
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

function buildOwnerFragmentsByOwnerId(ownerFragments, operationId) {
  const byOwnerId = new Map();
  for (const fragment of ownerFragments) {
    if (!fragment || typeof fragment.ownerId !== "string" || fragment.ownerId === "") {
      throw new Error(`Extract operation ${operationId} has invalid owner fragment ownerId`);
    }
    if (typeof fragment.id !== "string" || fragment.id === "") {
      throw new Error(`Extract operation ${operationId} has invalid owner fragment id`);
    }
    if (!Array.isArray(fragment.declaratorIndices) || fragment.declaratorIndices.length === 0) {
      throw new Error(`Extract operation ${operationId} has invalid owner fragment declaratorIndices`);
    }
    if (!byOwnerId.has(fragment.ownerId)) {
      byOwnerId.set(fragment.ownerId, []);
    }
    byOwnerId.get(fragment.ownerId).push({
      ...fragment,
      declaratorIndices: [...fragment.declaratorIndices],
      memberNames: [...(fragment.memberNames ?? [])],
    });
  }
  for (const fragments of byOwnerId.values()) {
    fragments.sort(
      (left, right) => (left.orderIndex ?? 0) - (right.orderIndex ?? 0) || left.id.localeCompare(right.id)
    );
  }
  return byOwnerId;
}

function buildRemainingProgramValidationIndex(analysis) {
  const earlierEagerUsersByOwnerId = new Map();
  const potentialUsersByOwnerId = new Map();
  const writersByOwnerId = new Map();
  for (const record of [...analysis.owners, ...analysis.sideEffects]) {
    indexRemainingProgramAccesses(writersByOwnerId, record, [
      ...record.writesTopLevel.eager,
      ...record.writesTopLevel.lazy,
    ]);
    indexRemainingProgramAccesses(earlierEagerUsersByOwnerId, record, [
      ...record.readsTopLevel.eager,
      ...record.memberWritesTopLevel.eager,
    ]);
    indexRemainingProgramAccesses(potentialUsersByOwnerId, record, [
      ...record.readsTopLevel.eager,
      ...record.readsTopLevel.lazy,
      ...record.memberWritesTopLevel.eager,
      ...record.memberWritesTopLevel.lazy,
    ]);
  }
  return {
    earlierEagerUsersByOwnerId,
    potentialUsersByOwnerId,
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
  const ownerFragmentsByOwnerId = buildOwnerFragmentsByOwnerId(operation.selector.ownerFragments ?? [], operation.id);
  for (const ownerId of operation.selector.ownerIds ?? []) {
    const owner = ownerById.get(ownerId);
    if (!owner) {
      return false;
    }
    if (owner.currentExtractorCompatible === false) {
      if (owner.type === "VariableDeclaration" && (ownerFragmentsByOwnerId.get(ownerId)?.length ?? 0) > 0) {
        continue;
      }
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
      if (!index.has(ownerId)) {
        index.set(ownerId, []);
      }
      index.get(ownerId).push(entry);
    }
  }
  return index;
}

function finalizeResolvedEntryImports(entry, resolvedByOwnerId) {
  const importsByTargetFile = new Map();
  for (const [ownerId, names] of entry.usedExtractedDependencyNames ?? []) {
    for (const name of names) {
      const providerEntry = resolveDependencyProviderEntry(resolvedByOwnerId.get(ownerId) ?? [], entry, name);
      if (!providerEntry) {
        continue;
      }
      if (!importsByTargetFile.has(providerEntry.targetFile)) {
        importsByTargetFile.set(providerEntry.targetFile, new Set());
      }
      importsByTargetFile.get(providerEntry.targetFile).add(
        JSON.stringify({
          imported: exportNameForLocal(providerEntry, name),
          local: name,
        })
      );
    }
  }
  entry.usedExtractedImports = [...importsByTargetFile.entries()]
    .map(([sourceTargetFile, names]) => ({
      sourceTargetFile,
      specifiers: [...names].sort().map((encodedSpecifier) => JSON.parse(encodedSpecifier)),
    }))
    .sort((left, right) => left.sourceTargetFile.localeCompare(right.sourceTargetFile));
}

function resolveDependencyProviderEntry(providerEntries, consumingEntry, localName) {
  const candidates = providerEntries.filter(
    (providerEntry) => providerEntry.id !== consumingEntry.id && providerEntry.exportedNames.includes(localName)
  );
  if (candidates.length === 0) {
    return null;
  }
  if (candidates.length > 1) {
    throw new Error(
      `Ambiguous extracted dependency provider for ${localName}: ${candidates.map((entry) => entry.id).join(", ")}`
    );
  }
  return candidates[0];
}

function finalizeBindingPlacements(bindingPlacements, operationId) {
  const placementsBySourceName = new Map();
  for (const placement of bindingPlacements) {
    if (!placement || typeof placement.sourceName !== "string" || placement.sourceName === "") {
      throw new Error(`Extract operation ${operationId} has invalid binding placement sourceName`);
    }
    if (typeof placement.name !== "string" || placement.name === "") {
      throw new Error(
        `Extract operation ${operationId} has invalid binding placement name for ${placement.sourceName}`
      );
    }
    const existing = placementsBySourceName.get(placement.sourceName);
    if (existing && existing.name !== placement.name) {
      throw new Error(
        `Extract operation ${operationId} assigns conflicting final names to ${placement.sourceName}: ${existing.name} vs ${placement.name}`
      );
    }
    placementsBySourceName.set(placement.sourceName, {
      ...placement,
    });
  }
  return [...placementsBySourceName.values()].sort((left, right) => left.sourceName.localeCompare(right.sourceName));
}

function finalizeExportBindings(exportedNames, bindingPlacements, operationId) {
  const exportBindings = exportedNames.map((local) => ({
    exported: local,
    local,
  }));
  const exportBindingByLocal = new Map(exportBindings.map((binding) => [binding.local, binding]));
  for (const placement of bindingPlacements) {
    const binding = exportBindingByLocal.get(placement.sourceName);
    if (!binding) {
      continue;
    }
    binding.exported = placement.name;
  }
  const duplicateExportNames = findDuplicateStrings(exportBindings.map((binding) => binding.exported));
  if (duplicateExportNames.length > 0) {
    throw new Error(
      `Extract operation ${operationId} assigns duplicate exported logical names: ${duplicateExportNames.join(", ")}`
    );
  }
  return exportBindings;
}

function normalizeAtomicBoundaryUnits(atomicBoundaryUnits) {
  return atomicBoundaryUnits
    .map((unit) => ({
      attachedItemIds: [...(unit.attachedItemIds ?? [])].sort(),
      id: unit.id,
      memberNames: [...(unit.memberNames ?? [])].sort(),
      ownerIds: [...(unit.ownerIds ?? [])],
      ownerFragments: [...(unit.ownerFragments ?? [])]
        .map((fragment) => ({
          declaratorIndices: [...(fragment.declaratorIndices ?? [])],
          id: fragment.id,
          kind: fragment.kind,
          memberNames: [...(fragment.memberNames ?? [])].sort(),
          orderIndex: fragment.orderIndex ?? 0,
          ownerId: fragment.ownerId,
        }))
        .sort(
          (left, right) =>
            left.ownerId.localeCompare(right.ownerId) ||
            left.orderIndex - right.orderIndex ||
            left.id.localeCompare(right.id)
        ),
      startOrdinal: unit.startOrdinal ?? Number.POSITIVE_INFINITY,
      unitIds: [...(unit.unitIds ?? [])],
    }))
    .sort((left, right) => left.startOrdinal - right.startOrdinal || left.id.localeCompare(right.id));
}

function exportNameForLocal(entry, localName) {
  return entry.exportBindings?.find((binding) => binding.local === localName)?.exported ?? localName;
}

function findDuplicateStrings(values) {
  const seen = new Set();
  const duplicates = new Set();
  for (const value of values) {
    if (seen.has(value)) {
      duplicates.add(value);
      continue;
    }
    seen.add(value);
  }
  return [...duplicates].sort();
}

const SUPPORTED_OWNER_TYPES = new Set(["FunctionDeclaration", "ClassDeclaration", "VariableDeclaration"]);
const EXTRACT_OPERATION_TYPES = new Set(["lower_selected_module_region", PLAN_SELECTED_MODULE_GROUPS_OPERATION]);
const RUNTIME_CONSTRUCTOR_SHADOW_NONE = 0;
const RUNTIME_CONSTRUCTOR_SHADOW_WORKER = 1;
const RUNTIME_CONSTRUCTOR_SHADOW_SHARED_WORKER = 2;

export const extractOrderedInitRegionsInCode = lowerSelectedModuleRegionsInCode;
export const extractOrderedInitRegionsInAst = lowerSelectedModuleRegionsInAst;
