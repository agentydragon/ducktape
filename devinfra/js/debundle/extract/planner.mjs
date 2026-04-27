import { packOrderedInitOwnerClosures, planOrderedInitOwnerClosureExtractions } from "./decl_graph.mjs";

const SELECTED_ATOMIC_UNIT_ID_PREFIX = "selected_atomic_unit_";
const ATOMIC_MODULE_ID_PREFIX = "atomic_module_";
const ANALYSIS_OWNER_BY_ID_CACHE = new WeakMap();
const ANALYSIS_ITEM_BY_ID_CACHE = new WeakMap();
const ANALYSIS_DEFAULT_SELECTED_OWNER_IDS_CACHE = new WeakMap();

export function planGuidedSelectedOwnerModules(
  { analysis, code, itemMetricsById = null, programBody = null },
  {
    maxModuleLines = 20_000,
    minModuleLines = 500,
    selectedOwnerIds: explicitSelectedOwnerIds = null,
  } = {}
) {
  if (!analysis?.owners || !analysis?.programItems) {
    throw new Error("planGuidedSelectedOwnerModules requires analysis");
  }
  if (!Array.isArray(programBody) && !(itemMetricsById instanceof Map)) {
    throw new Error("planGuidedSelectedOwnerModules requires programBody or itemMetricsById");
  }

  const ownerById = getOwnerByIdForAnalysis(analysis);
  const selectedOwnerIds = explicitSelectedOwnerIds
    ? requireKnownOwnerIds(explicitSelectedOwnerIds, ownerById, "planGuidedSelectedOwnerModules explicit selectedOwnerIds")
    : selectedOwnerIdsFromDefaultClosureSelection(analysis, ownerById, "planGuidedSelectedOwnerModules");
  const itemById = getItemByIdForAnalysis(analysis);
  const selectedAtomicUnits = buildSelectedAtomicUnits({
    analysis,
    ownerById,
    selectedOwnerIds,
  }).map((unit, index) =>
    finalizeAtomicUnit(unit, {
      code,
      id: `${SELECTED_ATOMIC_UNIT_ID_PREFIX}${index.toString().padStart(4, "0")}`,
      index,
      itemMetricsById,
      itemById,
      ownerById,
      programBody,
    })
  );

  const modulePlans = mergeAtomicUnitsIntoModules(selectedAtomicUnits, {
    maxModuleLines,
    minModuleLines,
  }).map((modulePlan, index) =>
    finalizeModulePlan(modulePlan, {
      id: `guided_selected_owner_module_${index.toString().padStart(4, "0")}`,
      index,
      ownerById,
    })
  );

  return {
    kind: "js.guided_selected_owner_module_plan",
    atomicUnitCount: selectedAtomicUnits.length,
    atomicUnits: selectedAtomicUnits,
    maxModuleLines,
    minModuleLines,
    modulePlans,
    selectedOwnerCount: selectedOwnerIds.size,
  };
}

export function planSelectedAtomicModules(
  { analysis, code, itemMetricsById = null, programBody = null },
  { selectedOwnerIds: explicitSelectedOwnerIds = null } = {}
) {
  if (!analysis?.owners || !analysis?.programItems) {
    throw new Error("planSelectedAtomicModules requires analysis");
  }
  if (!Array.isArray(programBody) && !(itemMetricsById instanceof Map)) {
    throw new Error("planSelectedAtomicModules requires programBody or itemMetricsById");
  }

  const startedAt = process.hrtime.bigint();
  const ownerById = getOwnerByIdForAnalysis(analysis);
  const selectionStartedAt = process.hrtime.bigint();
  const selectedOwnerIds = explicitSelectedOwnerIds
    ? requireKnownOwnerIds(explicitSelectedOwnerIds, ownerById, "planSelectedAtomicModules explicit selectedOwnerIds")
    : selectedOwnerIdsFromDefaultClosureSelection(analysis, ownerById, "planSelectedAtomicModules");
  const selectionMs = durationMsSince(selectionStartedAt);
  const itemById = getItemByIdForAnalysis(analysis);
  const buildUnitsStartedAt = process.hrtime.bigint();
  const rawAtomicUnits = buildSelectedAtomicUnits({
    analysis,
    ownerById,
    selectedOwnerIds,
  });
  const buildUnitsMs = durationMsSince(buildUnitsStartedAt);
  const finalizeUnitsStartedAt = process.hrtime.bigint();
  const atomicUnits = rawAtomicUnits.map((unit, index) =>
    finalizeAtomicUnit(unit, {
      code,
      id: `${SELECTED_ATOMIC_UNIT_ID_PREFIX}${index.toString().padStart(4, "0")}`,
      index,
      itemMetricsById,
      itemById,
      ownerById,
      programBody,
    })
  );
  const finalizeUnitsMs = durationMsSince(finalizeUnitsStartedAt);

  const finalizeModulesStartedAt = process.hrtime.bigint();
  const modulePlans = atomicUnits.map((atomicUnit, index) =>
    finalizeModulePlan(newModuleFromAtomicUnit(atomicUnit), {
      id: `${ATOMIC_MODULE_ID_PREFIX}${index.toString().padStart(4, "0")}`,
      index,
      ownerById,
    })
  );
  const finalizeModulesMs = durationMsSince(finalizeModulesStartedAt);

  return {
    kind: "js.atomic_module_plan",
    atomicUnitCount: atomicUnits.length,
    atomicUnits,
    modulePlans,
    selectedOwnerCount: selectedOwnerIds.size,
    timingsMs: {
      buildAtomicUnits: buildUnitsMs,
      finalizeAtomicUnits: finalizeUnitsMs,
      finalizeModules: finalizeModulesMs,
      selectOwners: selectionMs,
      total: durationMsSince(startedAt),
    },
  };
}

export function buildSelectedModuleOperations(plan, options = {}) {
  const chunkId = options.chunkId ?? "<chunk>";
  const file = options.file ? normalizeRelativeFile(options.file) : null;
  const targetDir = normalizeRelativeFile(options.targetDir ?? "regions");
  const idPrefix = options.idPrefix ?? "selected_module";
  const filePrefix = options.filePrefix ?? "";
  const initPrefix = options.initPrefix ?? "init_";

  return plan.modulePlans.map((modulePlan, index) => {
    const target = deriveSelectedModuleTarget(modulePlan, index, { filePrefix, initPrefix, targetDir });
    return {
      id: `${idPrefix}__${modulePlan.id}`,
      graphGenerated: true,
      lowering: "staged_shell",
      operation: "extract_ordered_init_region",
      selector: {
        attachedItemIds: [...modulePlan.attachedItemIds],
        chunkId,
        ownerIds: [...modulePlan.ownerIds],
        ...(file ? { file } : {}),
      },
      target: {
        file: target.file,
        init: target.init,
      },
    };
  });
}

export function buildGuidedSelectedOwnerModuleOperations(plan, options = {}) {
  return buildSelectedModuleOperations(plan, {
    ...options,
    filePrefix: options.filePrefix ?? "guided_",
    idPrefix: options.idPrefix ?? "guided_selected_owner_module",
    initPrefix: options.initPrefix ?? "init_guided_",
  });
}

export function deriveSelectedModuleTarget(
  modulePlan,
  index,
  { filePrefix = "", initPrefix = "init_", targetDir = "modules" } = {}
) {
  const normalizedTargetDir = normalizeRelativeFile(targetDir);
  const basename = modulePlan.basename ?? `${modulePlan.id}__${modulePlan.nameHint ?? `module_${index}`}`;
  return {
    basename,
    file: modulePlan.targetFile ?? `${normalizedTargetDir}/${filePrefix}${basename}.js`,
    init: modulePlan.initName ?? sanitizeIdentifier(`${initPrefix}${basename}`),
  };
}

function selectedOwnerIdsFromDefaultClosureSelection(analysis, ownerById, callerName) {
  const cachedSelectedOwnerIds = ANALYSIS_DEFAULT_SELECTED_OWNER_IDS_CACHE.get(analysis);
  if (cachedSelectedOwnerIds) {
    return cachedSelectedOwnerIds;
  }
  const plan = planOrderedInitOwnerClosureExtractions(analysis);
  const packed = packOrderedInitOwnerClosures(plan, { lowering: "staged_shell" });
  const selectedOwnerIds = requireKnownOwnerIds(
    packed.batchPlans.flatMap((batchPlan) => batchPlan.ownerIds),
    ownerById,
    `${callerName} default closure selection`
  );
  ANALYSIS_DEFAULT_SELECTED_OWNER_IDS_CACHE.set(analysis, selectedOwnerIds);
  return selectedOwnerIds;
}

function getOwnerByIdForAnalysis(analysis) {
  const cached = ANALYSIS_OWNER_BY_ID_CACHE.get(analysis);
  if (cached) {
    return cached;
  }
  const ownerById = new Map();
  for (const owner of analysis.owners) {
    ownerById.set(owner.id, owner);
  }
  ANALYSIS_OWNER_BY_ID_CACHE.set(analysis, ownerById);
  return ownerById;
}

function getItemByIdForAnalysis(analysis) {
  const cached = ANALYSIS_ITEM_BY_ID_CACHE.get(analysis);
  if (cached) {
    return cached;
  }
  const itemById = new Map();
  for (const item of analysis.programItems) {
    itemById.set(item.id, item);
  }
  ANALYSIS_ITEM_BY_ID_CACHE.set(analysis, itemById);
  return itemById;
}

function requireKnownOwnerIds(ownerIds, ownerById, source) {
  // `analysis.owners` is the authoritative owner universe for a boundary-analysis
  // snapshot. Selected-owner sets must be subsets of that universe. If we ever see
  // an unknown id here, that indicates either a bad caller-supplied selector set or
  // an internal planner inconsistency, and we want to fail at the boundary instead
  // of silently dropping it and masking the source of the corruption.
  const normalizedOwnerIds = new Set(ownerIds);
  const unknownOwnerIds = [...normalizedOwnerIds].filter((ownerId) => !ownerById.has(ownerId));
  if (unknownOwnerIds.length > 0) {
    const sample = unknownOwnerIds.slice(0, 8).join(", ");
    const remainder = unknownOwnerIds.length > 8 ? ` (+${unknownOwnerIds.length - 8} more)` : "";
    throw new Error(`${source} referenced unknown owner ids outside analysis.owners: ${sample}${remainder}`);
  }
  return normalizedOwnerIds;
}

function buildSelectedAtomicUnits({ analysis, ownerById, selectedOwnerIds }) {
  requireKnownOwnerIds(selectedOwnerIds, ownerById, "buildSelectedAtomicUnits selectedOwnerIds");
  const mustLinkAdjacency = new Map([...selectedOwnerIds].map((ownerId) => [ownerId, new Set()]));
  const replayableSideEffects = [];
  const linkOwners = (leftOwnerId, rightOwnerId) => {
    if (leftOwnerId === rightOwnerId) {
      return;
    }
    mustLinkAdjacency.get(leftOwnerId)?.add(rightOwnerId);
    mustLinkAdjacency.get(rightOwnerId)?.add(leftOwnerId);
  };

  const selectedOwners = [...selectedOwnerIds]
    .map((ownerId) => ownerById.get(ownerId))
    .sort((left, right) => left.ordinal - right.ordinal);

  for (const owner of selectedOwners) {
    forEachTopLevelAccess(owner, (access, bucket, phase) => {
      if (access.kind !== "local_declaration" || !access.ownerId || !selectedOwnerIds.has(access.ownerId)) {
        return true;
      }
      if (bucket === "reads") {
        if (phase !== "eager") {
          return true;
        }
        if (access.ownerId === owner.id) {
          return true;
        }
        const targetOwner = ownerById.get(access.ownerId);
        if (targetOwner && targetOwner.ordinal > owner.ordinal) {
          linkOwners(owner.id, targetOwner.id);
        }
        return true;
      }
      linkOwners(owner.id, access.ownerId);
      return true;
    });
  }

  for (const sideEffect of analysis.sideEffects) {
    if (!isReplayableAttachedSideEffectNode(sideEffect)) {
      continue;
    }
    const touchedOwnerIds = touchedSelectedOwnerIds(sideEffect, selectedOwnerIds);
    if (!touchedOwnerIds) {
      continue;
    }
    replayableSideEffects.push({ touchedOwnerIds, sideEffectId: sideEffect.id });
    if (touchedOwnerIds.length < 2) {
      continue;
    }
    for (let index = 1; index < touchedOwnerIds.length; index++) {
      linkOwners(touchedOwnerIds[0], touchedOwnerIds[index]);
    }
  }

  const visited = new Set();
  const units = [];
  for (const owner of selectedOwners) {
    if (visited.has(owner.id)) {
      continue;
    }
    const ownerIds = [];
    const stack = [owner.id];
    visited.add(owner.id);
    while (stack.length > 0) {
      const currentOwnerId = stack.pop();
      ownerIds.push(currentOwnerId);
      for (const dependencyOwnerId of mustLinkAdjacency.get(currentOwnerId) ?? []) {
        if (visited.has(dependencyOwnerId)) {
          continue;
        }
        visited.add(dependencyOwnerId);
        stack.push(dependencyOwnerId);
      }
    }
    ownerIds.sort((left, right) => ownerById.get(left).ordinal - ownerById.get(right).ordinal);
    units.push({
      attachedItemIds: [],
      ownerIds,
    });
  }

  const unitIndexByOwnerId = new Map();
  units.forEach((unit, index) => {
    for (const ownerId of unit.ownerIds) {
      unitIndexByOwnerId.set(ownerId, index);
    }
  });

  for (const { touchedOwnerIds, sideEffectId } of replayableSideEffects) {
    if (touchedOwnerIds.length === 0) {
      continue;
    }
    const firstUnitIndex = unitIndexByOwnerId.get(touchedOwnerIds[0]);
    if (firstUnitIndex === undefined) {
      continue;
    }
    let sameUnit = true;
    for (let index = 1; index < touchedOwnerIds.length; index++) {
      if (unitIndexByOwnerId.get(touchedOwnerIds[index]) !== firstUnitIndex) {
        sameUnit = false;
        break;
      }
    }
    if (!sameUnit) {
      continue;
    }
    units[firstUnitIndex].attachedItemIds.push(sideEffectId);
  }

  return units;
}

function touchedSelectedOwnerIds(sideEffect, selectedOwnerIds) {
  const touchedOwnerIds = [];
  const touchedOwnerIdSet = new Set();
  const seenNonSelected = forEachTopLevelAccess(sideEffect, (access) => {
    if (access.kind !== "local_declaration" || !access.ownerId) {
      return true;
    }
    if (!selectedOwnerIds.has(access.ownerId)) {
      return false;
    }
    if (!touchedOwnerIdSet.has(access.ownerId)) {
      touchedOwnerIdSet.add(access.ownerId);
      touchedOwnerIds.push(access.ownerId);
    }
    return true;
  });
  if (!seenNonSelected) {
    return null;
  }
  return touchedOwnerIds;
}

function finalizeAtomicUnit(unit, { code, id, index, itemMetricsById, itemById, ownerById, programBody }) {
  const itemIds = [...unit.ownerIds, ...unit.attachedItemIds];
  let lines = 0;
  let bytes = typeof code === "string" ? 0 : null;
  let startOrdinal = Number.POSITIVE_INFINITY;
  for (const itemId of itemIds) {
    const metrics = statementMetricForItem(itemId, { code, itemMetricsById, itemById, programBody });
    lines += metrics.lines;
    if (bytes !== null) {
      bytes += metrics.bytes;
    }
    const ordinal = itemById.get(itemId)?.ordinal ?? Number.POSITIVE_INFINITY;
    if (ordinal < startOrdinal) {
      startOrdinal = ordinal;
    }
  }
  return {
    attachedItemIds: [...unit.attachedItemIds],
    bytes,
    id,
    index,
    lines,
    memberNames: unit.ownerIds.flatMap((ownerId) => ownerById.get(ownerId)?.names ?? []).sort(),
    ownerIds: [...unit.ownerIds],
    startOrdinal,
  };
}

function statementMetricForItem(itemId, { code, itemMetricsById, itemById, programBody }) {
  const fromIndex = itemMetricsById?.get(itemId);
  if (fromIndex) {
    return {
      bytes: fromIndex.bytes ?? 0,
      lines: fromIndex.lines ?? 0,
    };
  }
  return {
    bytes: statementByteCountForItem(itemId, { code, itemById, programBody }),
    lines: statementLineCountForItem(itemId, { itemById, programBody }),
  };
}

function mergeAtomicUnitsIntoModules(atomicUnits, { maxModuleLines, minModuleLines }) {
  const modules = [];
  let currentModule = null;

  for (const atomicUnit of atomicUnits) {
    if (!currentModule) {
      currentModule = newModuleFromAtomicUnit(atomicUnit);
      continue;
    }

    const nextLineCount = currentModule.lines + atomicUnit.lines;
    if (currentModule.lines < minModuleLines && nextLineCount <= maxModuleLines) {
      mergeAtomicUnitIntoModule(currentModule, atomicUnit);
      continue;
    }
    if (currentModule.lines < minModuleLines) {
      mergeAtomicUnitIntoModule(currentModule, atomicUnit);
      continue;
    }

    modules.push(currentModule);
    currentModule = newModuleFromAtomicUnit(atomicUnit);
  }

  if (currentModule) {
    modules.push(currentModule);
  }
  return modules;
}

function newModuleFromAtomicUnit(atomicUnit) {
  return {
    attachedItemIds: [...atomicUnit.attachedItemIds],
    bytes: atomicUnit.bytes,
    lines: atomicUnit.lines,
    memberNames: [...atomicUnit.memberNames],
    ownerIds: [...atomicUnit.ownerIds],
    startOrdinal: atomicUnit.startOrdinal,
    unitIds: [atomicUnit.id],
  };
}

function mergeAtomicUnitIntoModule(modulePlan, atomicUnit) {
  modulePlan.attachedItemIds.push(...atomicUnit.attachedItemIds);
  modulePlan.bytes = modulePlan.bytes === null || atomicUnit.bytes === null ? null : modulePlan.bytes + atomicUnit.bytes;
  modulePlan.lines += atomicUnit.lines;
  modulePlan.memberNames.push(...atomicUnit.memberNames);
  modulePlan.ownerIds.push(...atomicUnit.ownerIds);
  modulePlan.unitIds.push(atomicUnit.id);
}

function finalizeModulePlan(modulePlan, { id, index, ownerById, basename = undefined }) {
  const uniqueMemberNames = [...new Set(modulePlan.memberNames)].sort();
  const nameHint = moduleNameHint(uniqueMemberNames, index);
  return {
    attachedItemIds: [...new Set(modulePlan.attachedItemIds)].sort(),
    basename: sanitizeIdentifier(basename ?? `${id}__${nameHint}`),
    bytes: modulePlan.bytes,
    id,
    index,
    lines: modulePlan.lines,
    memberNames: uniqueMemberNames,
    nameHint,
    ownerIds: [...new Set(modulePlan.ownerIds)].sort(
      (leftOwnerId, rightOwnerId) => ownerById.get(leftOwnerId).ordinal - ownerById.get(rightOwnerId).ordinal
    ),
    startOrdinal: modulePlan.startOrdinal,
    unitIds: [...modulePlan.unitIds],
  };
}

function moduleNameHint(memberNames, index) {
  const descriptiveNames = memberNames.filter(isDescriptiveModuleName).slice(0, 3);
  const sourceNames = descriptiveNames.length > 0 ? descriptiveNames : memberNames.slice(0, 3);
  const hint = sourceNames.join("_");
  return sanitizeIdentifier(hint || `module_${index.toString().padStart(4, "0")}`);
}

function isDescriptiveModuleName(name) {
  return /[A-Z_]/.test(name) || name.length >= 5;
}

function statementLineCountForItem(itemId, { itemById, programBody }) {
  const item = itemById.get(itemId);
  const statement = programBody[item?.ordinal];
  if (!statement?.loc) {
    return 0;
  }
  return statement.loc.end.line - statement.loc.start.line + 1;
}

function statementByteCountForItem(itemId, { code, itemById, programBody }) {
  const item = itemById.get(itemId);
  const statement = programBody[item?.ordinal];
  if (typeof statement?.start !== "number" || typeof statement?.end !== "number") {
    return 0;
  }
  return Buffer.byteLength(code.slice(statement.start, statement.end));
}

function orderedInitEagerReadAccesses(record) {
  return topLevelAccesses(record, "reads", "eager");
}

function orderedInitLazyReadAccesses(record) {
  return topLevelAccesses(record, "reads", "lazy");
}

function orderedInitWriteAccesses(record) {
  return topLevelAccesses(record, "writes", "eager");
}

function orderedInitLazyWriteAccesses(record) {
  return topLevelAccesses(record, "writes", "lazy");
}

function orderedInitEagerMemberWriteAccesses(record) {
  return topLevelAccesses(record, "memberWrites", "eager");
}

function orderedInitLazyMemberWriteAccesses(record) {
  return topLevelAccesses(record, "memberWrites", "lazy");
}

function topLevelAccesses(record, bucket, phase) {
  const finalized = record?.[`${bucket}TopLevel`]?.[phase];
  if (Array.isArray(finalized)) {
    return finalized;
  }
  const rawBucketName = `${phase}${bucket[0].toUpperCase()}${bucket.slice(1)}`;
  const rawBucket = record?.[rawBucketName];
  if (!rawBucket) {
    return [];
  }
  if (typeof rawBucket.values === "function") {
    return [...rawBucket.values()];
  }
  if (Array.isArray(rawBucket)) {
    return rawBucket;
  }
  return [];
}

function forEachTopLevelAccess(record, callback) {
  for (const access of orderedInitEagerReadAccesses(record)) {
    if (callback(access, "reads", "eager") === false) {
      return false;
    }
  }
  for (const access of orderedInitLazyReadAccesses(record)) {
    if (callback(access, "reads", "lazy") === false) {
      return false;
    }
  }
  for (const access of orderedInitWriteAccesses(record)) {
    if (callback(access, "writes", "eager") === false) {
      return false;
    }
  }
  for (const access of orderedInitLazyWriteAccesses(record)) {
    if (callback(access, "writes", "lazy") === false) {
      return false;
    }
  }
  for (const access of orderedInitEagerMemberWriteAccesses(record)) {
    if (callback(access, "memberWrites", "eager") === false) {
      return false;
    }
  }
  for (const access of orderedInitLazyMemberWriteAccesses(record)) {
    if (callback(access, "memberWrites", "lazy") === false) {
      return false;
    }
  }
  return true;
}

function isReplayableAttachedSideEffectNode(sideEffectNodeOrRecord) {
  const type = sideEffectNodeOrRecord?.type ?? sideEffectNodeOrRecord?.node?.type ?? null;
  if (type !== "ExpressionStatement") {
    return false;
  }
  return !(
    sideEffectNodeOrRecord?.effects?.containsDirectEval ||
    sideEffectNodeOrRecord?.effects?.containsImportMeta ||
    sideEffectNodeOrRecord?.effects?.containsTopLevelAwait
  );
}

function normalizeRelativeFile(value) {
  return value.replace(/^\.\/+/, "").replace(/\\/g, "/");
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
