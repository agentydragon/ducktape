export const PLAN_SELECTED_MODULE_GROUPS_OPERATION = "plan_selected_module_groups";
const SELECTED_MODULE_SUPPORTED_OWNER_TYPES = new Set(["FunctionDeclaration", "ClassDeclaration", "VariableDeclaration"]);
const RUNTIME_SENSITIVE_EFFECTS = new Set(["containsDirectEval", "containsImportMeta", "containsTopLevelAwait"]);
const SELECTED_MODULE_ACCESS_VIEW_CACHE = new WeakMap();
const SELECTED_MODULE_PLANNER_STATE_CACHE = new WeakMap();
const SELECTED_MODULE_STAGED_SHELL_OWNER_ADJACENCY_CACHE = new WeakMap();

export function expandSelectedModuleGroupPlanningOperations(analysis, operations, context = {}) {
  const expanded = [];
  const selectedOwnerIds = new Set();
  const selectedAttachedItemIds = new Set();

  const rememberSelection = (operation) => {
    if (operation.operation !== "lower_selected_module_region") {
      return;
    }
    for (const ownerId of operation.selector.ownerIds ?? []) {
      selectedOwnerIds.add(ownerId);
    }
    for (const itemId of operation.selector.attachedItemIds ?? []) {
      selectedAttachedItemIds.add(itemId);
    }
  };

  for (const operation of operations) {
    if (operation.operation !== PLAN_SELECTED_MODULE_GROUPS_OPERATION) {
      expanded.push(operation);
      rememberSelection(operation);
      continue;
    }
    validateOwnerClosurePassOperationShape(operation);
    const selectorFile = operation.selector.file
      ? normalizeRelativeFile(operation.selector.file)
      : context.file
        ? normalizeRelativeFile(context.file)
        : null;
    const selectorChunkId = operation.selector.chunkId ?? context.chunkId;
    if (selectorChunkId !== analysis.chunkId) {
      continue;
    }
    if (selectorFile && context.file && selectorFile !== normalizeRelativeFile(context.file)) {
      continue;
    }

    const planningOptions = { ...(operation.options ?? {}) };
    delete planningOptions.lowering;
    delete planningOptions.preselectedClosureIds;
    const packed = packSelectedModuleGroups(
      planSelectedModuleGroupExtractions(analysis, planningOptions),
      operation.options ?? {}
    );
    const targetDir = normalizeRelativeFile(operation.target.dir ?? "regions");
    const filePrefix = operation.target.filePrefix ?? "";
    const initPrefix = operation.target.initPrefix ?? "init_";
    const generated = buildSelectedModuleGroupOperations(packed, {
      chunkId: selectorChunkId,
      file: selectorFile,
      filePrefix,
      idPrefix: operation.id,
      initPrefix,
      lowering: packed.lowering,
      targetDir,
    });

    for (const generatedOperation of generated) {
      if (generatedOperation.selector.ownerIds.some((ownerId) => selectedOwnerIds.has(ownerId))) {
        continue;
      }
      if ((generatedOperation.selector.attachedItemIds ?? []).some((itemId) => selectedAttachedItemIds.has(itemId))) {
        continue;
      }
      expanded.push(generatedOperation);
      rememberSelection(generatedOperation);
    }
  }

  return expanded;
}

export function planSelectedModuleGroupExtractions(analysis, options = {}) {
  const ownerById = new Map(analysis.owners.map((owner) => [owner.id, owner]));
  const programItemByOrdinal = new Map(analysis.programItems.map((item) => [item.ordinal, item]));
  const plannerState = getOrderedInitPlannerState(analysis, ownerById);
  const includeReportDetails = options.includeReportDetails === true;
  const {
    componentById,
    componentByOwnerId,
    components,
  } = buildOwnerDependencyComponents(analysis, ownerById);
  const closureComponentIdsBySeedComponentId = new Map();

  const closurePlans = dedupeOwnerClosurePlans(
    components.map((component, index) =>
      buildOwnerClosurePlan(component, {
        analysis,
        closureComponentIdsBySeedComponentId,
        componentById,
        componentByOwnerId,
        includeReportDetails,
        index,
        options,
        ownerById,
        plannerState,
        programItemByOrdinal,
      })
    )
  );
  return {
    kind: "js.selected_module_group_plan",
    analysisContext: {
      owners: analysis.owners,
      programItems: analysis.programItems,
      sideEffects: analysis.sideEffects,
    },
    componentPlans: components,
    closurePlans,
  };
}

export function packSelectedModuleGroups(plan, options = {}) {
  const startedAt = process.hrtime.bigint();
  const lowering = options.lowering ?? "staged_shell";
  if (lowering !== "staged_shell") {
    throw new Error(`Unsupported selected-module group lowering: ${lowering}`);
  }
  const { candidateBatchPlans, timingsMs: buildTimingsMs } = buildStagedShellBatchPlans(plan, options);
  const selectStartedAt = process.hrtime.bigint();
  const batchPlans = selectPackedOwnerClosureBatchPlans(candidateBatchPlans, options);
  return {
    kind: "js.selected_module_group_batch_plan",
    lowering,
    candidateBatchPlans,
    batchPlans,
    timingsMs: {
      ...buildTimingsMs,
      selectPacked: durationMsSince(selectStartedAt),
      total: durationMsSince(startedAt),
    },
  };
}

export function buildSelectedModuleGroupOperations(planOrBatchPlan, options = {}) {
  const chunkId = options.chunkId ?? "<chunk>";
  const file = options.file ? normalizeRelativeFile(options.file) : null;
  const targetDir = normalizeRelativeFile(options.targetDir ?? "regions");
  const filePrefix = options.filePrefix ?? "selected_module_group_";
  const initPrefix = options.initPrefix ?? "init_selected_module_group_";
  const lowering = options.lowering ?? "staged_shell";
  if (lowering !== "staged_shell") {
    throw new Error(`Unsupported selected-module group lowering: ${lowering}`);
  }
  const batchPlans = planOrBatchPlan.batchPlans ?? packSelectedModuleGroups(planOrBatchPlan, options).batchPlans;
  return batchPlans.map((batchPlan) => ({
      id: `${options.idPrefix ?? "selected_module_group"}__${batchPlan.id}`,
      graphGenerated: true,
      lowering: batchPlan.lowering ?? lowering,
      operation: "lower_selected_module_region",
      selector: {
        attachedItemIds: [...(batchPlan.attachedItemIds ?? [])],
        chunkId,
        ownerIds: [...batchPlan.ownerIds],
        ...(file ? { file } : {}),
      },
      target: {
        file: `${targetDir}/${filePrefix}${batchPlan.id}.js`,
        init: sanitizeIdentifier(`${initPrefix}${batchPlan.id}`),
      },
    }));
}

function buildOwnerDependencyComponents(analysis, ownerById) {
  const adjacency = new Map(analysis.owners.map((owner) => [owner.id, new Set()]));
  const edgeRecords = new Map();

  for (const owner of analysis.owners) {
    for (const dependency of selectedModuleAccessView(owner).all) {
      if (dependency.kind !== "local_declaration" || !dependency.ownerId || !ownerById.has(dependency.ownerId)) {
        continue;
      }
      adjacency.get(owner.id).add(dependency.ownerId);
      const key = `${owner.id}->${dependency.ownerId}`;
      let edgeRecord = edgeRecords.get(key);
      if (!edgeRecord) {
        edgeRecord = {
          fromOwnerId: owner.id,
          toOwnerId: dependency.ownerId,
          names: new Set(),
          phases: new Set(),
          accessKinds: new Set(),
        };
        edgeRecords.set(key, edgeRecord);
      }
      edgeRecord.names.add(dependency.name);
      edgeRecord.phases.add(dependency.phase);
      edgeRecord.accessKinds.add(dependency.accessKind);
    }
  }

  const stronglyConnected = stronglyConnectedComponents(
    analysis.owners.map((owner) => owner.id),
    adjacency
  )
    .map((ownerIds) => ownerIds.map((ownerId) => ownerById.get(ownerId)).filter(Boolean))
    .filter((owners) => owners.length > 0)
    .sort(
      (left, right) =>
        Math.min(...left.map((owner) => owner.ordinal)) - Math.min(...right.map((owner) => owner.ordinal))
    );

  const components = stronglyConnected.map((componentOwners, index) => {
    const sortedOwners = [...componentOwners].sort((left, right) => left.ordinal - right.ordinal);
    return {
      id: `owner_component_${index.toString().padStart(4, "0")}`,
      cyclic:
        sortedOwners.length > 1 ||
        sortedOwners.some((owner) => adjacency.get(owner.id)?.has(owner.id)),
      dependencies: [],
      directDependencyComponentIds: [],
      endOrdinal: sortedOwners.at(-1).ordinal,
      memberNames: sortedOwners.flatMap((owner) => owner.names).sort(),
      ownerIds: sortedOwners.map((owner) => owner.id),
      startOrdinal: sortedOwners[0].ordinal,
    };
  });

  const componentById = new Map(components.map((component) => [component.id, component]));
  const componentByOwnerId = new Map();
  for (const component of components) {
    for (const ownerId of component.ownerIds) {
      componentByOwnerId.set(ownerId, component.id);
    }
  }

  const dependencyIndexByComponentId = new Map(components.map((component) => [component.id, new Map()]));
  for (const edgeRecord of edgeRecords.values()) {
    const fromComponentId = componentByOwnerId.get(edgeRecord.fromOwnerId);
    const toComponentId = componentByOwnerId.get(edgeRecord.toOwnerId);
    if (!fromComponentId || !toComponentId || fromComponentId === toComponentId) {
      continue;
    }
    const componentDependencies = dependencyIndexByComponentId.get(fromComponentId);
    let dependencyRecord = componentDependencies.get(toComponentId);
    if (!dependencyRecord) {
      dependencyRecord = {
        toComponentId,
        names: new Set(),
        phases: new Set(),
        accessKinds: new Set(),
      };
      componentDependencies.set(toComponentId, dependencyRecord);
    }
    for (const name of edgeRecord.names) {
      dependencyRecord.names.add(name);
    }
    for (const phase of edgeRecord.phases) {
      dependencyRecord.phases.add(phase);
    }
    for (const accessKind of edgeRecord.accessKinds) {
      dependencyRecord.accessKinds.add(accessKind);
    }
  }

  for (const component of components) {
    const dependencies = [...dependencyIndexByComponentId.get(component.id).values()]
      .map((dependencyRecord) => ({
        toComponentId: dependencyRecord.toComponentId,
        names: [...dependencyRecord.names].sort(),
        phases: [...dependencyRecord.phases].sort(),
        accessKinds: [...dependencyRecord.accessKinds].sort(),
      }))
      .sort((left, right) => left.toComponentId.localeCompare(right.toComponentId));
    component.dependencies = dependencies;
    component.directDependencyComponentIds = dependencies.map((dependency) => dependency.toComponentId);
  }

  return {
    componentById,
    componentByOwnerId,
    components,
  };
}

function buildOwnerClosurePlan(
  seedComponent,
  {
    analysis,
    closureComponentIdsBySeedComponentId,
    componentById,
    componentByOwnerId,
    includeReportDetails,
    index,
    options,
    ownerById,
    plannerState,
    programItemByOrdinal,
  }
) {
  const requiredClosureComponentIds = collectOwnerComponentClosureIds(
    seedComponent.id,
    componentById,
    closureComponentIdsBySeedComponentId
  );
  const semanticOwners = ownersForComponentIds(requiredClosureComponentIds, componentById, ownerById);
  const semanticSummary = summarizeOwnerClosureEnvelope({
    owners: analysis.owners,
    ownerById,
    plannerState,
    regionOwners: semanticOwners,
    sideEffects: analysis.sideEffects,
  });
  const plan = {
    blockingReasons: [...semanticSummary.selectedModuleBlockingReasons],
    closureComponentIds: [...requiredClosureComponentIds],
    directDependencyComponentIds: [...seedComponent.directDependencyComponentIds],
    endOrdinal: semanticSummary.endOrdinal,
    estimatedSize: estimatedRegionSize(semanticSummary),
    id: `selected_module_group_${index.toString().padStart(4, "0")}`,
    lineSpan: lineSpanForRegion(semanticSummary),
    memberNames: [...semanticSummary.memberNames],
    ownerIds: [...semanticSummary.ownerIds],
    requiredClosureComponentIds: [...requiredClosureComponentIds],
    requiredClosureOwnerIds: semanticOwners.map((owner) => owner.id),
    seedComponentId: seedComponent.id,
    seedMemberNames: [...seedComponent.memberNames],
    seedOwnerIds: [...seedComponent.ownerIds],
    startOrdinal: semanticSummary.startOrdinal,
  };

  if (!includeReportDetails) {
    return plan;
  }

  return {
    ...plan,
    ...buildOwnerClosureReportDetails(requiredClosureComponentIds, {
      analysis,
      componentById,
      componentByOwnerId,
      options,
      ownerById,
      plannerState,
      programItemByOrdinal,
      seedComponent,
      semanticSummary,
    }),
  };
}

function buildOwnerClosureReportDetails(
  requiredClosureComponentIds,
  {
    analysis,
    componentById,
    componentByOwnerId,
    options,
    ownerById,
    plannerState,
    programItemByOrdinal,
    seedComponent,
    semanticSummary,
  }
) {
  const contiguousEnvelope = buildContiguousOwnerClosureEnvelope(requiredClosureComponentIds, {
    analysis,
    componentById,
    componentByOwnerId,
    options,
    ownerById,
    plannerState,
    programItemByOrdinal,
    seedComponent,
  });
  const envelopeAddedOwnerIds = contiguousEnvelope.summary.ownerIds.filter(
    (ownerId) => !semanticSummary.ownerIds.includes(ownerId)
  );

  return {
    contiguousEnvelopeBlockingReasons: [...contiguousEnvelope.blockingReasons],
    contiguousEnvelopeComponentIds: [...contiguousEnvelope.componentIds],
    contiguousEnvelopeEndOrdinal: contiguousEnvelope.summary.endOrdinal,
    contiguousEnvelopeEstimatedSize: estimatedRegionSize(contiguousEnvelope.summary),
    contiguousEnvelopeMemberNames: [...contiguousEnvelope.summary.memberNames],
    contiguousEnvelopeOwnerIds: [...contiguousEnvelope.summary.ownerIds],
    contiguousEnvelopeStartOrdinal: contiguousEnvelope.summary.startOrdinal,
    envelopeAddedMemberNames: envelopeAddedOwnerIds.flatMap((ownerId) => ownerById.get(ownerId)?.names ?? []).sort(),
    envelopeAddedOwnerIds: [...new Set(envelopeAddedOwnerIds)].sort(),
  };
}

function dedupeOwnerClosurePlans(closurePlans) {
  const byOwnerSignature = new Map();
  for (const plan of closurePlans) {
    const signature = plan.ownerIds.join(",");
    const existing = byOwnerSignature.get(signature);
    if (!existing) {
      byOwnerSignature.set(signature, plan);
      continue;
    }
    if (compareOwnerClosurePlans(plan, existing) < 0) {
      byOwnerSignature.set(signature, plan);
    }
  }
  return [...byOwnerSignature.values()].sort(compareOwnerClosurePlans);
}

function compareOwnerClosurePlans(left, right) {
  return (
    left.blockingReasons.length - right.blockingReasons.length ||
    right.estimatedSize - left.estimatedSize ||
    right.ownerIds.length - left.ownerIds.length ||
    left.startOrdinal - right.startOrdinal ||
    left.id.localeCompare(right.id)
  );
}

function buildStagedShellBatchPlans(plan, options) {
  const startedAt = process.hrtime.bigint();
  const { analysisContext } = plan;
  if (!analysisContext) {
    throw new Error("Owner closure plan is missing analysisContext for staged-shell lowering");
  }
  const ownerById = new Map(analysisContext.owners.map((owner) => [owner.id, owner]));
  const plannerState = getOrderedInitPlannerState(analysisContext, ownerById);
  const ownerAdjacency = getStagedShellOwnerAdjacency(analysisContext, ownerById);
  const recordById = new Map([
    ...analysisContext.owners.map((owner) => [owner.id, owner]),
    ...analysisContext.sideEffects.map((sideEffect) => [sideEffect.id, sideEffect]),
  ]);
  const programItemByOrdinal = new Map(analysisContext.programItems.map((item) => [item.ordinal, item]));
  const timingTotals = {
    collectAttachableSideEffectIds: 0,
    expandStagedAttachedOwners: 0,
    finalizeBatchPlan: 0,
    shellScan: 0,
    stageRuns: 0,
    summarizeEnvelope: 0,
  };
  const buildCandidatePlansStartedAt = process.hrtime.bigint();
  const candidateBatchPlans = [...plan.closurePlans]
    .map((closurePlan) =>
      buildStagedShellBatchPlan(closurePlan, {
        options,
        owners: analysisContext.owners,
        ownerById,
        ownerAdjacency,
        plannerState,
        programItemByOrdinal,
        recordById,
        sideEffects: analysisContext.sideEffects,
        timingTotals,
      })
    );
  const buildCandidatePlansMs = durationMsSince(buildCandidatePlansStartedAt);
  const sortCandidatePlansStartedAt = process.hrtime.bigint();
  candidateBatchPlans.sort(compareOwnerClosureBatchPlans);
  return {
    candidateBatchPlans,
    timingsMs: {
      buildCandidatePlans: buildCandidatePlansMs,
      sortCandidatePlans: durationMsSince(sortCandidatePlansStartedAt),
      totalBuildCandidates: durationMsSince(startedAt),
      ...timingTotals,
    },
  };
}

function selectPackedOwnerClosureBatchPlans(candidateBatchPlans, options) {
  const selected = [];
  const selectedIds = new Set(options.preselectedClosureIds ?? []);
  const occupiedOwnerIds = new Set();
  const occupiedItemIds = new Set();

  for (const batchPlan of candidateBatchPlans) {
    if (!selectedIds.has(batchPlan.id)) {
      continue;
    }
    selected.push(batchPlan);
    for (const ownerId of batchPlan.ownerIds) {
      occupiedOwnerIds.add(ownerId);
    }
    for (const itemId of batchPlan.attachedItemIds ?? []) {
      occupiedItemIds.add(itemId);
    }
  }

  for (const batchPlan of candidateBatchPlans) {
    if (selectedIds.has(batchPlan.id) || batchPlan.blockingReasons.length > 0) {
      continue;
    }
    if (batchPlan.ownerIds.some((ownerId) => occupiedOwnerIds.has(ownerId))) {
      continue;
    }
    if ((batchPlan.attachedItemIds ?? []).some((itemId) => occupiedItemIds.has(itemId))) {
      continue;
    }
    selected.push(batchPlan);
    for (const ownerId of batchPlan.ownerIds) {
      occupiedOwnerIds.add(ownerId);
    }
    for (const itemId of batchPlan.attachedItemIds ?? []) {
      occupiedItemIds.add(itemId);
    }
  }

  return selected.sort((left, right) => left.startOrdinal - right.startOrdinal || left.id.localeCompare(right.id));
}

function buildStagedShellBatchPlan(
  plan,
  { options, owners, ownerAdjacency, ownerById, plannerState, programItemByOrdinal, recordById, sideEffects, timingTotals }
) {
  const expandStartedAt = process.hrtime.bigint();
  const expandedOwners = expandStagedAttachedOwners(plan.ownerIds, { ownerAdjacency, ownerById });
  const expandedOwnerIds = expandedOwners.map((owner) => owner.id);
  recordTiming(timingTotals, "expandStagedAttachedOwners", durationMsSince(expandStartedAt));
  const collectSideEffectsStartedAt = process.hrtime.bigint();
  const attachedSideEffectIds = collectAttachableSideEffectIds(expandedOwnerIds, {
    ownerById,
    plannerState,
    sideEffects,
  });
  recordTiming(timingTotals, "collectAttachableSideEffectIds", durationMsSince(collectSideEffectsStartedAt));
  const selectedItemIds = new Set([...expandedOwnerIds, ...attachedSideEffectIds]);
  const selectedItems = buildSelectedItems(expandedOwners, attachedSideEffectIds, recordById);

  const stageRunsStartedAt = process.hrtime.bigint();
  const stageRuns = buildStagedSelectedItemRuns(plan.id, selectedItems);
  recordTiming(timingTotals, "stageRuns", durationMsSince(stageRunsStartedAt));
  const summarizeStartedAt = process.hrtime.bigint();
  const summary = summarizeStagedAttachedEnvelope({
    attachedSideEffectIds,
    ownerById,
    owners,
    plannerState,
    regionOwners: expandedOwners,
  });
  recordTiming(timingTotals, "summarizeEnvelope", durationMsSince(summarizeStartedAt));

  const shellItemIds = [];
  const shellBlockingReasons = new Set();
  const firstOrdinal = selectedItems[0]?.ordinal ?? 0;
  const lastOrdinal = selectedItems.at(-1)?.ordinal ?? firstOrdinal;
  const shellScanStartedAt = process.hrtime.bigint();
  for (let ordinal = firstOrdinal; ordinal <= lastOrdinal; ordinal++) {
    const item = programItemByOrdinal.get(ordinal);
    if (!item || selectedItemIds.has(item.id)) {
      continue;
    }
    shellItemIds.push(item.id);
    const record = recordById.get(item.id);
    if (!record) {
      continue;
    }
    for (const access of selectedModuleAccessView(record).eagerReadLike) {
      if (access.kind !== "local_declaration" || !access.ownerId || !summary.selectedOwnerIds.has(access.ownerId)) {
        continue;
      }
      const targetOwner = ownerById.get(access.ownerId);
      if (!targetOwner || targetOwner.ordinal <= item.ordinal) {
        continue;
      }
      shellBlockingReasons.add(`shell_item_eagerly_uses_later_owner:${item.id}:${access.ownerId}`);
    }
  }
  recordTiming(timingTotals, "shellScan", durationMsSince(shellScanStartedAt));

  if (typeof options.minLineSpan === "number" && lineSpanForRegion(summary) < options.minLineSpan) {
    shellBlockingReasons.add(`below_min_line_span:${options.minLineSpan}`);
  }

  const addedOwnerIds = expandedOwnerIds.filter((ownerId) => !plan.ownerIds.includes(ownerId));
  const finalizeStartedAt = process.hrtime.bigint();
  const batchPlan = {
    attachedItemIds: [...attachedSideEffectIds],
    blockingReasons: [...new Set([...summary.selectedModuleBlockingReasons, ...shellBlockingReasons])].sort(),
    closureComponentIds: [...plan.closureComponentIds],
    directDependencyComponentIds: [...plan.directDependencyComponentIds],
    endOrdinal: summary.endOrdinal,
    envelopeAddedMemberNames: addedOwnerIds.flatMap((ownerId) => ownerById.get(ownerId)?.names ?? []).sort(),
    envelopeAddedOwnerIds: [...new Set(addedOwnerIds)].sort(),
    estimatedSize: estimatedRegionSize(summary),
    id: plan.id,
    lineSpan: lineSpanForRegion(summary),
    lowering: "staged_shell",
    memberNames: [...summary.memberNames],
    ownerIds: [...expandedOwnerIds],
    seedComponentId: plan.seedComponentId,
    seedMemberNames: [...plan.seedMemberNames],
    seedOwnerIds: [...plan.seedOwnerIds],
    semanticBlockingReasons: [...summary.selectedModuleBlockingReasons],
    semanticMemberNames: [...summary.memberNames],
    semanticOwnerIds: [...expandedOwnerIds],
    shellItemIds: [...shellItemIds],
    stageRuns,
    startOrdinal: summary.startOrdinal,
  };
  recordTiming(timingTotals, "finalizeBatchPlan", durationMsSince(finalizeStartedAt));
  return batchPlan;
}

function compareOwnerClosureBatchPlans(left, right) {
  return (
    left.blockingReasons.length - right.blockingReasons.length ||
    right.estimatedSize - left.estimatedSize ||
    right.semanticOwnerIds.length - left.semanticOwnerIds.length ||
    left.startOrdinal - right.startOrdinal ||
    left.id.localeCompare(right.id)
  );
}

function expandStagedAttachedOwners(seedOwnerIds, { ownerAdjacency, ownerById }) {
  const selectedOwnerIds = new Set(seedOwnerIds);
  const stack = [...selectedOwnerIds];
  while (stack.length > 0) {
    const ownerId = stack.pop();
    for (const adjacentOwnerId of ownerAdjacency.get(ownerId) ?? []) {
      if (selectedOwnerIds.has(adjacentOwnerId)) {
        continue;
      }
      selectedOwnerIds.add(adjacentOwnerId);
      stack.push(adjacentOwnerId);
    }
  }
  return [...selectedOwnerIds]
    .map((ownerId) => ownerById.get(ownerId))
    .filter(Boolean)
    .sort((left, right) => left.ordinal - right.ordinal);
}

function getStagedShellOwnerAdjacency(analysis, ownerById) {
  const cached = SELECTED_MODULE_STAGED_SHELL_OWNER_ADJACENCY_CACHE.get(analysis);
  if (cached) {
    return cached;
  }
  const adjacency = new Map(analysis.owners.map((owner) => [owner.id, new Set()]));
  for (const owner of analysis.owners) {
    const accessView = selectedModuleAccessView(owner);
    const ownerAdjacency = adjacency.get(owner.id);
    for (const access of accessView.all) {
      if (access.kind !== "local_declaration" || !access.ownerId || !ownerById.has(access.ownerId)) {
        continue;
      }
      ownerAdjacency.add(access.ownerId);
    }
    for (const access of accessView.writeLike) {
      if (access.kind !== "local_declaration" || !access.ownerId || !ownerById.has(access.ownerId)) {
        continue;
      }
      adjacency.get(access.ownerId)?.add(owner.id);
    }
  }
  SELECTED_MODULE_STAGED_SHELL_OWNER_ADJACENCY_CACHE.set(analysis, adjacency);
  return adjacency;
}

function collectAttachableSideEffectIds(selectedOwnerIds, { ownerById, plannerState, sideEffects }) {
  const selectedOwnerIdSet = new Set(selectedOwnerIds);
  const resolvedPlannerState =
    plannerState ?? getOrderedInitPlannerState({ owners: [...ownerById.values()], sideEffects }, ownerById);
  const candidateSideEffectIds = new Set();
  for (const ownerId of selectedOwnerIdSet) {
    for (const sideEffectId of resolvedPlannerState.replayableSideEffectIdsByTouchedOwnerId.get(ownerId) ?? []) {
      candidateSideEffectIds.add(sideEffectId);
    }
  }
  return [...candidateSideEffectIds]
    .filter((sideEffectId) => {
      const sideEffectState = resolvedPlannerState.replayableSideEffectStateById.get(sideEffectId);
      if (!sideEffectState || sideEffectState.runtimeSensitive || sideEffectState.touchedOwnerIds.length === 0) {
        return false;
      }
      return sideEffectState.touchedOwnerIds.every(
        (ownerId) => selectedOwnerIdSet.has(ownerId) && ownerById.has(ownerId)
      );
    })
    .sort();
}

function isReplayableAttachedSideEffectNode(sideEffectNodeOrRecord) {
  const type = sideEffectNodeOrRecord?.type ?? sideEffectNodeOrRecord?.node?.type ?? null;
  if (type === "ExpressionStatement") {
    return true;
  }
  return false;
}

function buildStagedSelectedItemRuns(batchId, selectedItems) {
  const stageRuns = [];
  for (const item of selectedItems) {
    const currentStage = stageRuns.at(-1);
    if (!currentStage || currentStage.endOrdinal + 1 !== item.ordinal) {
      stageRuns.push({
        endOrdinal: item.ordinal,
        id: `${batchId}_stage_${stageRuns.length}`,
        itemIds: [item.id],
        memberNames: [...item.memberNames],
        ownerIds: [...item.ownerIds],
        startOrdinal: item.ordinal,
      });
      continue;
    }
    currentStage.endOrdinal = item.ordinal;
    currentStage.itemIds.push(item.id);
    currentStage.memberNames.push(...item.memberNames);
    currentStage.ownerIds.push(...item.ownerIds);
  }
  return stageRuns.map((stageRun) => ({
    ...stageRun,
    memberNames: [...new Set(stageRun.memberNames)].sort(),
    ownerIds: [...new Set(stageRun.ownerIds)].sort(),
  }));
}

function buildSelectedItems(expandedOwners, attachedSideEffectIds, recordById) {
  const selectedSideEffects = attachedSideEffectIds
    .map((sideEffectId) => recordById.get(sideEffectId))
    .filter(Boolean)
    .sort((left, right) => left.ordinal - right.ordinal || left.id.localeCompare(right.id));
  const selectedItems = [];
  let ownerIndex = 0;
  let sideEffectIndex = 0;

  while (ownerIndex < expandedOwners.length || sideEffectIndex < selectedSideEffects.length) {
    const owner = expandedOwners[ownerIndex] ?? null;
    const sideEffect = selectedSideEffects[sideEffectIndex] ?? null;
    if (
      owner &&
      (!sideEffect ||
        owner.ordinal < sideEffect.ordinal ||
        (owner.ordinal === sideEffect.ordinal && owner.id.localeCompare(sideEffect.id) <= 0))
    ) {
      selectedItems.push({
        id: owner.id,
        kind: "declaration",
        memberNames: [...owner.names],
        ordinal: owner.ordinal,
        ownerIds: [owner.id],
      });
      ownerIndex++;
      continue;
    }
    selectedItems.push({
      id: sideEffect.id,
      kind: "side_effect",
      memberNames: [],
      ordinal: sideEffect.ordinal,
      ownerIds: [],
    });
    sideEffectIndex++;
  }

  return selectedItems;
}

function summarizeStagedAttachedEnvelope({ attachedSideEffectIds, ownerById, owners, plannerState, regionOwners }) {
  const selectedOwnerIds = new Set(regionOwners.map((owner) => owner.id));
  const selectedItemIds = new Set([...selectedOwnerIds, ...attachedSideEffectIds]);
  const selectedAttachedSideEffectIds = new Set(attachedSideEffectIds);
  const selectedAttachedSideEffects = [...selectedAttachedSideEffectIds]
    .map((sideEffectId) => plannerState.recordById.get(sideEffectId))
    .filter(Boolean);
  let startOrdinal = regionOwners[0]?.ordinal ?? 0;
  let endOrdinal = regionOwners.at(-1)?.ordinal ?? startOrdinal;
  for (const sideEffect of selectedAttachedSideEffects) {
    if (sideEffect.ordinal < startOrdinal) {
      startOrdinal = sideEffect.ordinal;
    }
    if (sideEffect.ordinal > endOrdinal) {
      endOrdinal = sideEffect.ordinal;
    }
  }
  const outsideLocalDependencyOwnerIds = new Set();
  const earlierEagerUseItemIds = new Set();
  const outsideWriteItemIds = new Set();
  const runtimeSensitiveOwnerIds = new Set();
  const extractorIncompatibleOwnerIds = new Set();
  const unsupportedOwnerIds = new Set();
  const unsupportedForwardEdges = new Set();
  const unsupportedRuntimeImportWrites = new Set();
  const runtimeSensitiveSideEffectIds = new Set();

  const selectedFunctionOwnerIds = new Set(
    regionOwners.filter((owner) => owner.type === "FunctionDeclaration").map((owner) => owner.id)
  );

  for (const owner of regionOwners) {
    if (!SELECTED_MODULE_SUPPORTED_OWNER_TYPES.has(owner.type)) {
      unsupportedOwnerIds.add(owner.id);
    }
    if (owner.currentExtractorCompatible === false) {
      extractorIncompatibleOwnerIds.add(owner.id);
    }
    if (isRuntimeSensitiveRecord(owner)) {
      runtimeSensitiveOwnerIds.add(owner.id);
    }

    const accessView = selectedModuleAccessView(owner);
    for (const access of accessView.all) {
      if (noteRuntimeImportAccess(access, unsupportedRuntimeImportWrites)) {
        continue;
      }
      if (access.kind !== "local_declaration" || !access.ownerId || selectedOwnerIds.has(access.ownerId)) {
        continue;
      }
      outsideLocalDependencyOwnerIds.add(access.ownerId);
    }

    for (const access of accessView.forwardDependencies) {
      if (access.kind !== "local_declaration" || !access.ownerId || !selectedOwnerIds.has(access.ownerId)) {
        continue;
      }
      if (access.ownerId === owner.id) {
        continue;
      }
      const targetOwner = ownerById.get(access.ownerId);
      if (!targetOwner) {
        continue;
      }
      if (selectedFunctionOwnerIds.has(targetOwner.id) || targetOwner.ordinal < owner.ordinal) {
        continue;
      }
      unsupportedForwardEdges.add(`${owner.id}->${targetOwner.id}`);
    }
  }

  for (const sideEffect of selectedAttachedSideEffects) {
    if (isRuntimeSensitiveRecord(sideEffect)) {
      runtimeSensitiveSideEffectIds.add(sideEffect.id);
    }
    for (const access of selectedModuleAccessView(sideEffect).all) {
      if (noteRuntimeImportAccess(access, unsupportedRuntimeImportWrites)) {
        continue;
      }
      if (access.kind !== "local_declaration" || !access.ownerId || selectedOwnerIds.has(access.ownerId)) {
        continue;
      }
      outsideLocalDependencyOwnerIds.add(access.ownerId);
    }
  }

  collectOutsideRecordEffects({
    earlierEagerUseItemIds,
    outsideWriteItemIds,
    plannerState,
    selectedItemIds,
    selectedOwnerIds,
    startOrdinal,
  });

  const blockingReasons = buildSelectedModuleBlockingReasons({
    earlierEagerUseItemIds,
    extractorIncompatibleOwnerIds,
    outsideLocalDependencyOwnerIds,
    outsideWriteItemIds,
    runtimeSensitiveOwnerIds,
    runtimeSensitiveSideEffectIds,
    unsupportedForwardEdges,
    unsupportedOwnerIds,
    unsupportedRuntimeImportWrites,
  });

  return {
    endOrdinal,
    lines: regionOwners.map((owner) => owner.line).filter((line) => line !== null),
    memberNames: regionOwners.flatMap((owner) => owner.names).sort(),
    selectedModuleBlockingReasons: blockingReasons.sort(),
    ownerIds: regionOwners.map((owner) => owner.id),
    selectedOwnerIds,
    selectedItemIds,
    startOrdinal,
  };
}

function collectOwnerComponentClosureIds(componentId, componentById, cache = null) {
  if (cache?.has(componentId)) {
    return cache.get(componentId);
  }
  const closureIds = new Set([componentId]);
  const component = componentById.get(componentId);
  for (const dependencyComponentId of component?.directDependencyComponentIds ?? []) {
    for (const transitiveComponentId of collectOwnerComponentClosureIds(dependencyComponentId, componentById, cache)) {
      closureIds.add(transitiveComponentId);
    }
  }
  const sortedClosureIds = [...closureIds].sort();
  cache?.set(componentId, sortedClosureIds);
  return sortedClosureIds;
}

function ownersForComponentIds(componentIds, componentById, ownerById) {
  return [...componentIds]
    .flatMap((componentId) => componentById.get(componentId)?.ownerIds ?? [])
    .map((ownerId) => ownerById.get(ownerId))
    .filter(Boolean)
    .sort((left, right) => left.ordinal - right.ordinal);
}

function buildContiguousOwnerClosureEnvelope(
  requiredClosureComponentIds,
  { analysis, componentById, componentByOwnerId, options, ownerById, plannerState, programItemByOrdinal, seedComponent }
) {
  const selectedComponentIds = new Set(requiredClosureComponentIds);
  const selectedOwners = ownersForComponentIds(selectedComponentIds, componentById, ownerById);
  const envelopeBarrierItemIds = new Set();
  let startOrdinal = selectedOwners[0]?.ordinal ?? seedComponent.startOrdinal;
  let endOrdinal = selectedOwners.at(-1)?.ordinal ?? seedComponent.endOrdinal;

  let changed = true;
  while (changed && envelopeBarrierItemIds.size === 0) {
    changed = false;
    for (let ordinal = startOrdinal; ordinal <= endOrdinal; ordinal++) {
      const programItem = programItemByOrdinal.get(ordinal);
      if (!programItem || programItem.kind !== "declaration") {
        envelopeBarrierItemIds.add(programItem?.id ?? `missing_program_item:${ordinal}`);
        continue;
      }
      const componentId = componentByOwnerId.get(programItem.id);
      if (!componentId) {
        envelopeBarrierItemIds.add(`missing_component_for_owner:${programItem.id}`);
        continue;
      }
      if (!selectedComponentIds.has(componentId)) {
        selectedComponentIds.add(componentId);
        changed = true;
        const addedComponent = componentById.get(componentId);
        if (addedComponent) {
          for (const ownerId of addedComponent.ownerIds) {
            const addedOwner = ownerById.get(ownerId);
            if (addedOwner) {
              selectedOwners.push(addedOwner);
            }
          }
          if (addedComponent.startOrdinal < startOrdinal) {
            startOrdinal = addedComponent.startOrdinal;
          }
          if (addedComponent.endOrdinal > endOrdinal) {
            endOrdinal = addedComponent.endOrdinal;
          }
        }
      }
    }
  }

  const summary = summarizeOwnerClosureEnvelope({
    owners: analysis.owners,
    ownerById,
    plannerState,
    regionOwners: selectedOwners.sort((left, right) => left.ordinal - right.ordinal),
    sideEffects: analysis.sideEffects,
  });
  const blockingReasons = [
    ...summary.selectedModuleBlockingReasons,
    ...[...envelopeBarrierItemIds].sort().map((itemId) => `non_declaration_in_envelope:${itemId}`),
  ];
  if (typeof options.minLineSpan === "number" && lineSpanForRegion(summary) < options.minLineSpan) {
    blockingReasons.push(`below_min_line_span:${options.minLineSpan}`);
  }

  return {
    blockingReasons: [...new Set(blockingReasons)].sort(),
    componentIds: [...selectedComponentIds].sort(),
    summary,
  };
}

function summarizeOwnerClosureEnvelope({ owners, ownerById, plannerState, regionOwners, sideEffects }) {
  const selectedOwnerIds = new Set(regionOwners.map((owner) => owner.id));
  const selectedItemIds = selectedOwnerIds;
  const startOrdinal = regionOwners[0]?.ordinal ?? 0;
  const endOrdinal = regionOwners.at(-1)?.ordinal ?? startOrdinal;
  const outsideLocalDependencyOwnerIds = new Set();
  const earlierEagerUseItemIds = new Set();
  const outsideWriteItemIds = new Set();
  const runtimeSensitiveOwnerIds = new Set();
  const extractorIncompatibleOwnerIds = new Set();
  const unsupportedOwnerIds = new Set();
  const unsupportedForwardEdges = new Set();
  const unsupportedRuntimeImportWrites = new Set();

  const selectedFunctionOwnerIds = new Set(
    regionOwners.filter((owner) => owner.type === "FunctionDeclaration").map((owner) => owner.id)
  );

  for (const owner of regionOwners) {
    if (!SELECTED_MODULE_SUPPORTED_OWNER_TYPES.has(owner.type)) {
      unsupportedOwnerIds.add(owner.id);
    }
    if (owner.currentExtractorCompatible === false) {
      extractorIncompatibleOwnerIds.add(owner.id);
    }
    if (isRuntimeSensitiveRecord(owner)) {
      runtimeSensitiveOwnerIds.add(owner.id);
    }

    const accessView = selectedModuleAccessView(owner);
    for (const access of accessView.all) {
      if (noteRuntimeImportAccess(access, unsupportedRuntimeImportWrites)) {
        continue;
      }
      if (access.kind !== "local_declaration" || !access.ownerId || selectedOwnerIds.has(access.ownerId)) {
        continue;
      }
      outsideLocalDependencyOwnerIds.add(access.ownerId);
    }

    for (const access of accessView.forwardDependencies) {
      if (access.kind !== "local_declaration" || !access.ownerId || !selectedOwnerIds.has(access.ownerId)) {
        continue;
      }
      if (access.ownerId === owner.id) {
        continue;
      }
      const targetOwner = ownerById.get(access.ownerId);
      if (!targetOwner) {
        continue;
      }
      if (selectedFunctionOwnerIds.has(targetOwner.id) || targetOwner.ordinal < owner.ordinal) {
        continue;
      }
      unsupportedForwardEdges.add(`${owner.id}->${targetOwner.id}`);
    }
  }

  collectOutsideRecordEffects({
    earlierEagerUseItemIds,
    outsideWriteItemIds,
    plannerState,
    selectedItemIds,
    selectedOwnerIds,
    startOrdinal,
  });

  const blockingReasons = buildSelectedModuleBlockingReasons({
    earlierEagerUseItemIds,
    extractorIncompatibleOwnerIds,
    outsideLocalDependencyOwnerIds,
    outsideWriteItemIds,
    runtimeSensitiveOwnerIds,
    unsupportedForwardEdges,
    unsupportedOwnerIds,
    unsupportedRuntimeImportWrites,
  });

  return {
    endOrdinal,
    lines: regionOwners.map((owner) => owner.line).filter((line) => line !== null),
    memberNames: regionOwners.flatMap((owner) => owner.names).sort(),
    selectedModuleBlockingReasons: blockingReasons.sort(),
    ownerIds: regionOwners.map((owner) => owner.id),
    startOrdinal,
  };
}

function selectedModuleAccessView(record) {
  const cached = SELECTED_MODULE_ACCESS_VIEW_CACHE.get(record);
  if (cached) {
    return cached;
  }
  const eagerReads = normalizeTopLevelAccesses(record, "reads", "eager", "read");
  const lazyReads = normalizeTopLevelAccesses(record, "reads", "lazy", "read");
  const eagerWrites = normalizeTopLevelAccesses(record, "writes", "eager", "write");
  const lazyWrites = normalizeTopLevelAccesses(record, "writes", "lazy", "write");
  const eagerMemberWrites = normalizeTopLevelAccesses(record, "memberWrites", "eager", "member_write");
  const lazyMemberWrites = normalizeTopLevelAccesses(record, "memberWrites", "lazy", "member_write");
  const view = {
    all: [...eagerReads, ...lazyReads, ...eagerWrites, ...lazyWrites, ...eagerMemberWrites, ...lazyMemberWrites],
    eagerReadLike: [...eagerReads, ...eagerMemberWrites],
    forwardDependencies: [...eagerReads, ...eagerWrites, ...eagerMemberWrites],
    writes: [...eagerWrites, ...lazyWrites],
    writeLike: [...eagerWrites, ...lazyWrites, ...eagerMemberWrites, ...lazyMemberWrites],
  };
  SELECTED_MODULE_ACCESS_VIEW_CACHE.set(record, view);
  return view;
}

export function getOrderedInitPlannerStateForTesting(analysis, ownerById = null) {
  return getOrderedInitPlannerState(analysis, ownerById);
}

function getOrderedInitPlannerState(analysis, ownerById = null) {
  const cached = SELECTED_MODULE_PLANNER_STATE_CACHE.get(analysis);
  if (cached) {
    return cached;
  }

  const resolvedOwnerById = ownerById ?? new Map((analysis.owners ?? []).map((owner) => [owner.id, owner]));
  const allRecords = [...(analysis.owners ?? []), ...(analysis.sideEffects ?? [])];
  const writeRecordIdsByOwnerId = new Map((analysis.owners ?? []).map((owner) => [owner.id, []]));
  const eagerReadLikeRecordIdsByOwnerId = new Map((analysis.owners ?? []).map((owner) => [owner.id, []]));
  const replayableSideEffectIdsByTouchedOwnerId = new Map((analysis.owners ?? []).map((owner) => [owner.id, []]));
  const replayableSideEffectStateById = new Map();
  const recordById = new Map(allRecords.map((record) => [record.id, record]));

  for (const record of allRecords) {
    const accessView = selectedModuleAccessView(record);
    for (const access of accessView.writes) {
      if (access.kind !== "local_declaration" || !access.ownerId || !writeRecordIdsByOwnerId.has(access.ownerId)) {
        continue;
      }
      writeRecordIdsByOwnerId.get(access.ownerId).push(record.id);
    }
    for (const access of accessView.eagerReadLike) {
      if (
        access.kind !== "local_declaration" ||
        !access.ownerId ||
        !eagerReadLikeRecordIdsByOwnerId.has(access.ownerId)
      ) {
        continue;
      }
      eagerReadLikeRecordIdsByOwnerId.get(access.ownerId).push(record.id);
    }

    if (!isReplayableAttachedSideEffectNode(record)) {
      continue;
    }
    const touchedOwnerIds = [...new Set(
      selectedModuleAccessView(record).all
        .filter((access) => access.kind === "local_declaration" && access.ownerId && resolvedOwnerById.has(access.ownerId))
        .map((access) => access.ownerId)
    )].sort();
    if (touchedOwnerIds.length === 0) {
      continue;
    }
    replayableSideEffectStateById.set(record.id, {
      id: record.id,
      runtimeSensitive: isRuntimeSensitiveRecord(record),
      touchedOwnerIds,
    });
    for (const ownerId of touchedOwnerIds) {
      replayableSideEffectIdsByTouchedOwnerId.get(ownerId)?.push(record.id);
    }
  }

  const plannerState = {
    eagerReadLikeRecordIdsByOwnerId,
    recordById,
    replayableSideEffectIdsByTouchedOwnerId,
    replayableSideEffectStateById,
    writeRecordIdsByOwnerId,
  };
  SELECTED_MODULE_PLANNER_STATE_CACHE.set(analysis, plannerState);
  return plannerState;
}

function collectOutsideRecordEffects({
  earlierEagerUseItemIds,
  outsideWriteItemIds,
  plannerState,
  selectedItemIds,
  selectedOwnerIds,
  startOrdinal,
}) {
  for (const ownerId of selectedOwnerIds) {
    for (const recordId of plannerState.writeRecordIdsByOwnerId.get(ownerId) ?? []) {
      if (!selectedItemIds.has(recordId)) {
        outsideWriteItemIds.add(recordId);
      }
    }
    for (const recordId of plannerState.eagerReadLikeRecordIdsByOwnerId.get(ownerId) ?? []) {
      if (selectedItemIds.has(recordId)) {
        continue;
      }
      const record = plannerState.recordById.get(recordId);
      if (record && record.ordinal < startOrdinal) {
        earlierEagerUseItemIds.add(recordId);
      }
    }
  }
}

function recordTiming(timingTotals, key, durationMs) {
  if (!timingTotals) {
    return;
  }
  timingTotals[key] = (timingTotals[key] ?? 0) + durationMs;
}

function durationMsSince(startedAt) {
  return Number(process.hrtime.bigint() - startedAt) / 1_000_000;
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

function normalizeTopLevelAccesses(record, bucket, phase, accessKind) {
  return topLevelAccesses(record, bucket, phase).map((access) => ({
    ...access,
    accessKind,
    phase,
  }));
}

function noteRuntimeImportAccess(access, unsupportedRuntimeImportWrites) {
  if (access.kind !== "runtime_import") {
    return false;
  }
  if (access.accessKind === "write") {
    unsupportedRuntimeImportWrites.add(access.name);
  }
  return true;
}

function isRuntimeSensitiveRecord(record) {
  for (const effect of RUNTIME_SENSITIVE_EFFECTS) {
    if (record.effects?.[effect]) {
      return true;
    }
  }
  return false;
}

function buildSelectedModuleBlockingReasons({
  earlierEagerUseItemIds,
  extractorIncompatibleOwnerIds,
  outsideLocalDependencyOwnerIds,
  outsideWriteItemIds,
  runtimeSensitiveOwnerIds,
  runtimeSensitiveSideEffectIds = new Set(),
  unsupportedForwardEdges,
  unsupportedOwnerIds,
  unsupportedRuntimeImportWrites,
}) {
  return [
    ...(unsupportedOwnerIds.size > 0
      ? [`unsupported_owner:${[...unsupportedOwnerIds].sort().join(",")}`]
      : []),
    ...(runtimeSensitiveOwnerIds.size > 0
      ? [`runtime_sensitive_owner:${[...runtimeSensitiveOwnerIds].sort().join(",")}`]
      : []),
    ...(extractorIncompatibleOwnerIds.size > 0
      ? [`extractor_incompatible_owner:${[...extractorIncompatibleOwnerIds].sort().join(",")}`]
      : []),
    ...(runtimeSensitiveSideEffectIds.size > 0
      ? [`runtime_sensitive_side_effect:${[...runtimeSensitiveSideEffectIds].sort().join(",")}`]
      : []),
    ...(unsupportedRuntimeImportWrites.size > 0
      ? [`writes_runtime_import:${[...unsupportedRuntimeImportWrites].sort().join(",")}`]
      : []),
    ...(outsideLocalDependencyOwnerIds.size > 0
      ? [`depends_on_outside_local_owner:${[...outsideLocalDependencyOwnerIds].sort().join(",")}`]
      : []),
    ...(unsupportedForwardEdges.size > 0
      ? [`unsupported_forward_eager_dependency:${[...unsupportedForwardEdges].sort().join(",")}`]
      : []),
    ...(outsideWriteItemIds.size > 0 ? [`written_by_outside_item:${[...outsideWriteItemIds].sort().join(",")}`] : []),
    ...(earlierEagerUseItemIds.size > 0
      ? [`used_eagerly_before_region:${[...earlierEagerUseItemIds].sort().join(",")}`]
      : []),
  ];
}

function lineSpanForRegion(region) {
  if (!Array.isArray(region.lines) || region.lines.length === 0) {
    return region.ownerIds.length;
  }
  return region.lines.at(-1) - region.lines[0] + 1;
}

function estimatedRegionSize(region) {
  return lineSpanForRegion(region) * 1000 + region.ownerIds.length;
}

function sanitizeIdentifier(value) {
  return value
    .replace(/[^A-Za-z0-9_$]+/g, "_")
    .replace(/^[^A-Za-z_$]+/, "_")
    .replace(/_+/g, "_");
}

function stronglyConnectedComponents(nodes, edges) {
  let index = 0;
  const stack = [];
  const onStack = new Set();
  const indexByNode = new Map();
  const lowByNode = new Map();
  const components = [];

  function visit(node) {
    indexByNode.set(node, index);
    lowByNode.set(node, index);
    index++;
    stack.push(node);
    onStack.add(node);

    for (const next of edges.get(node) ?? []) {
      if (!indexByNode.has(next)) {
        visit(next);
        lowByNode.set(node, Math.min(lowByNode.get(node), lowByNode.get(next)));
      } else if (onStack.has(next)) {
        lowByNode.set(node, Math.min(lowByNode.get(node), indexByNode.get(next)));
      }
    }

    if (lowByNode.get(node) === indexByNode.get(node)) {
      const component = [];
      while (true) {
        const next = stack.pop();
        onStack.delete(next);
        component.push(next);
        if (next === node) {
          break;
        }
      }
      components.push(component);
    }
  }

  for (const node of nodes) {
    if (!indexByNode.has(node)) {
      visit(node);
    }
  }
  return components;
}

function validateOwnerClosurePassOperationShape(operation) {
  if (!operation?.id) {
    throw new Error("Owner closure extraction operation is missing id");
  }
  if (!operation.selector?.chunkId) {
    throw new Error(`Owner closure extraction operation ${operation.id} is missing selector.chunkId`);
  }
  if (!operation.target?.dir) {
    throw new Error(`Owner closure extraction operation ${operation.id} is missing target.dir`);
  }
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
