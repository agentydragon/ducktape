import * as t from "@babel/types";
import { analyzeVariableDeclarationFragmentAccesses } from "../analysis/boundary.mjs";
import {
  referencedUndeclaredNames,
  referencedUndeclaredNamesInVariableDeclarator,
} from "../common/program_analysis.mjs";
import { packSelectedModuleGroups, planSelectedModuleGroupExtractions } from "./decl_graph.mjs";

const SELECTED_ATOMIC_UNIT_ID_PREFIX = "selected_atomic_unit_";
const ATOMIC_MODULE_ID_PREFIX = "atomic_module_";
const GENERATED_INIT_PREFIX = "__dt_generated_init__";
const ANALYSIS_OWNER_BY_ID_CACHE = new WeakMap();
const ANALYSIS_ITEM_BY_ID_CACHE = new WeakMap();
const ANALYSIS_SIDE_EFFECT_BY_ID_CACHE = new WeakMap();
const ANALYSIS_DEFAULT_SELECTED_OWNER_IDS_CACHE = new WeakMap();

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
  const sideEffectById = getSideEffectByIdForAnalysis(analysis);
  const buildUnitsStartedAt = process.hrtime.bigint();
  const rawAtomicUnits = buildSelectedAtomicUnits({
    analysis,
    ownerById,
    selectedOwnerIds,
  });
  const expandedAtomicUnits = splitSplittableVariableDeclarationAtomicUnits(rawAtomicUnits, {
    analysis,
    ownerById,
    programBody,
    sideEffectById,
  });
  const buildUnitsMs = durationMsSince(buildUnitsStartedAt);
  const finalizeUnitsStartedAt = process.hrtime.bigint();
  const atomicUnits = expandedAtomicUnits.map((unit, index) =>
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

export function defaultSelectedOwnerIdsForAnalysis(analysis, callerName = "defaultSelectedOwnerIdsForAnalysis") {
  if (!analysis?.owners || !analysis?.programItems) {
    throw new Error(`${callerName} requires analysis`);
  }
  return selectedOwnerIdsFromDefaultClosureSelection(analysis, getOwnerByIdForAnalysis(analysis), callerName);
}

export function buildSelectedModuleOperations(plan, options = {}) {
  const chunkId = options.chunkId ?? "<chunk>";
  const file = options.file ? normalizeRelativeFile(options.file) : null;
  const targetDir = normalizeRelativeFile(options.targetDir ?? "regions");
  const idPrefix = options.idPrefix ?? "selected_module";
  const filePrefix = options.filePrefix ?? "";
  const initPrefix = options.initPrefix ?? GENERATED_INIT_PREFIX;

  return plan.modulePlans.map((modulePlan, index) => {
    const target = deriveSelectedModuleTarget(modulePlan, index, { filePrefix, initPrefix, targetDir });
    return {
      id: `${idPrefix}__${modulePlan.id}`,
      ...(Array.isArray(modulePlan.atomicBoundaryUnits)
        ? {
            atomicBoundaryUnits: modulePlan.atomicBoundaryUnits.map((unit) => ({
              attachedItemIds: [...unit.attachedItemIds],
              id: unit.id,
              memberNames: [...unit.memberNames],
              ownerIds: [...unit.ownerIds],
              ownerFragments: cloneOwnerFragments(unit.ownerFragments),
              startOrdinal: unit.startOrdinal,
              unitIds: [...unit.unitIds],
            })),
          }
        : {}),
      ...(Array.isArray(modulePlan.bindingPlacements)
        ? {
            bindingPlacements: modulePlan.bindingPlacements.map((entry) => ({ ...entry })),
          }
        : {}),
      graphGenerated: true,
      lowering: "staged_shell",
      operation: "lower_selected_module_region",
      selector: {
        attachedItemIds: [...modulePlan.attachedItemIds],
        chunkId,
        ownerIds: [...modulePlan.ownerIds],
        ...(Array.isArray(modulePlan.ownerFragments)
          ? {
              ownerFragments: cloneOwnerFragments(modulePlan.ownerFragments),
            }
          : {}),
        ...(file ? { file } : {}),
      },
      target: {
        file: target.file,
        init: target.init,
      },
    };
  });
}

export function deriveSelectedModuleTarget(
  modulePlan,
  index,
  { filePrefix = "", initPrefix = GENERATED_INIT_PREFIX, targetDir = "modules" } = {}
) {
  const normalizedTargetDir = normalizeOptionalRelativeDir(targetDir);
  const modulePath = modulePlan.modulePath ?? `${modulePlan.id}__${modulePlan.nameHint ?? `module_${index}`}`;
  return {
    file:
      modulePlan.targetFile ??
      (normalizedTargetDir ? `${normalizedTargetDir}/${filePrefix}${modulePath}.js` : `${filePrefix}${modulePath}.js`),
    init: modulePlan.initName ?? sanitizeIdentifier(`${initPrefix}${modulePath}`),
  };
}

function selectedOwnerIdsFromDefaultClosureSelection(analysis, ownerById, callerName) {
  const cachedSelectedOwnerIds = ANALYSIS_DEFAULT_SELECTED_OWNER_IDS_CACHE.get(analysis);
  if (cachedSelectedOwnerIds) {
    return cachedSelectedOwnerIds;
  }
  const plan = planSelectedModuleGroupExtractions(analysis);
  const packed = packSelectedModuleGroups(plan, { lowering: "staged_shell" });
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

function getSideEffectByIdForAnalysis(analysis) {
  const cached = ANALYSIS_SIDE_EFFECT_BY_ID_CACHE.get(analysis);
  if (cached) {
    return cached;
  }
  const sideEffectById = new Map();
  for (const sideEffect of analysis.sideEffects ?? []) {
    sideEffectById.set(sideEffect.id, sideEffect);
  }
  ANALYSIS_SIDE_EFFECT_BY_ID_CACHE.set(analysis, sideEffectById);
  return sideEffectById;
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

function splitSplittableVariableDeclarationAtomicUnits(
  rawAtomicUnits,
  { analysis, ownerById, programBody, sideEffectById }
) {
  const expanded = [];
  const localDeclarationNames = localDeclarationNamesForAnalysis(analysis);
  for (const unit of rawAtomicUnits) {
    const splitUnits = splitSplittableVariableDeclarationAtomicUnit(unit, {
      analysis,
      localDeclarationNames,
      ownerById,
      programBody,
      sideEffectById,
    });
    expanded.push(...(splitUnits ?? [unit]));
  }
  return expanded;
}

function splitSplittableVariableDeclarationAtomicUnit(
  unit,
  { analysis, localDeclarationNames, ownerById, programBody, sideEffectById }
) {
  if (unit.ownerIds.length !== 1) {
    return null;
  }
  const [ownerId] = unit.ownerIds;
  const owner = ownerById.get(ownerId);
  if (!owner || owner.type !== "VariableDeclaration") {
    return null;
  }
  const statement = programBody?.[owner.ordinal];
  const declaration = unwrapTopLevelDeclarationNode(statement);
  if (!t.isVariableDeclaration(declaration) || declaration.declarations.length <= 1) {
    return null;
  }
  const splitUnits = buildSplittableVariableDeclarationUnits(owner, declaration.declarations, {
    localDeclarationNames,
  });
  if (!splitUnits || splitUnits.length <= 1) {
    return null;
  }
  const dependencyDisjoint = buildVariableDeclarationFragmentDependencyDisjointSet(
    owner,
    declaration.declarations,
    splitUnits,
    {
      analysis,
      programBody,
      statement,
    }
  );
  if (!dependencyDisjoint) {
    return null;
  }
  if (unit.attachedItemIds.length === 0) {
    const groupedUnits = buildVariableDeclarationAtomicUnitsFromGroupedFragments(splitUnits, dependencyDisjoint, {
      owner,
    });
    return groupedUnits.length > 1 ? groupedUnits : null;
  }
  const groupedUnits = splitVariableDeclarationUnitWithAttachedSideEffects(unit, splitUnits, {
    owner,
    sideEffectById,
    dependencyDisjoint,
  });
  return groupedUnits && groupedUnits.length > 1 ? groupedUnits : null;
}

function buildSplittableVariableDeclarationUnits(owner, declarators, { localDeclarationNames } = {}) {
  const splittableFragments = [];
  const remainderDeclaratorIndices = [];
  for (const [index, declarator] of declarators.entries()) {
    const fragment = buildSplittableVariableDeclaratorFragment(owner, declarator, index, {
      localDeclarationNames,
    });
    if (fragment) {
      splittableFragments.push(fragment);
      continue;
    }
    remainderDeclaratorIndices.push(index);
  }
  if (splittableFragments.length === 0) {
    return null;
  }
  const fragments = [...splittableFragments];
  if (remainderDeclaratorIndices.length > 0) {
    fragments.push(buildGroupedVariableDeclaratorFragment(owner, declarators, remainderDeclaratorIndices));
  }
  return fragments.sort(
    (left, right) => (left.orderIndex ?? 0) - (right.orderIndex ?? 0) || left.id.localeCompare(right.id)
  );
}

function ownerHasLazyIntraOwnerBindingWrite(owner) {
  return selectedModuleLazyWriteAccesses(owner).some(
    (access) => access.kind === "local_declaration" && access.ownerId === owner.id
  );
}

function buildSplittableVariableDeclaratorFragment(owner, declarator, index, { localDeclarationNames } = {}) {
  const memberNames = bindingNamesForVariableDeclarator(declarator).sort();
  if (memberNames.length === 0) {
    return null;
  }
  if (
    !isStaticallyPureFragmentInitializer(declarator.init) &&
    !isLocallyPureFragmentInitializer(declarator.init, localDeclarationNames) &&
    !isLazyCallableFragmentInitializer(declarator.init) &&
    !isLazyClassFragmentInitializer(declarator.init)
  ) {
    return null;
  }
  return {
    declaratorIndices: [index],
    id: `${owner.id}::declarator_${index}`,
    kind: "variable_declarator",
    memberNames,
    orderIndex: index,
    ownerId: owner.id,
  };
}

function buildGroupedVariableDeclaratorFragment(owner, declarators, declaratorIndices) {
  return {
    declaratorIndices: [...declaratorIndices],
    id: `${owner.id}::declarator_group_${declaratorIndices.join("_")}`,
    kind: "variable_declarator_group",
    memberNames: declaratorIndices.flatMap((index) => bindingNamesForVariableDeclarator(declarators[index])).sort(),
    orderIndex: declaratorIndices[0] ?? 0,
    ownerId: owner.id,
  };
}

function splitVariableDeclarationUnitWithAttachedSideEffects(
  unit,
  fragments,
  { dependencyDisjoint, owner, sideEffectById }
) {
  if (!(sideEffectById instanceof Map)) {
    return null;
  }
  const fragmentIndexByMemberName = new Map();
  const fragmentIndexByDeclaratorIndex = new Map();
  fragments.forEach((fragment, fragmentIndex) => {
    for (const memberName of fragment.memberNames) {
      fragmentIndexByMemberName.set(memberName, fragmentIndex);
    }
    for (const declaratorIndex of fragment.declaratorIndices ?? []) {
      fragmentIndexByDeclaratorIndex.set(declaratorIndex, fragmentIndex);
    }
  });
  const disjoint = cloneDisjointSet(dependencyDisjoint ?? createDisjointSet(fragments.length));
  const sideEffects = [];
  for (const sideEffectId of unit.attachedItemIds) {
    const sideEffect = sideEffectById.get(sideEffectId);
    if (!sideEffect) {
      return null;
    }
    const touchedFragmentIndices = touchedFragmentIndicesForSideEffect(sideEffect, {
      fragmentIndexByMemberName,
      ownerId: owner.id,
    });
    if (!touchedFragmentIndices || touchedFragmentIndices.length === 0) {
      return null;
    }
    for (let index = 1; index < touchedFragmentIndices.length; index++) {
      disjoint.union(touchedFragmentIndices[0], touchedFragmentIndices[index]);
    }
    sideEffects.push({
      sideEffectId,
      touchedFragmentIndices,
    });
  }
  const attachedItemIdsByRoot = new Map();
  for (const { sideEffectId, touchedFragmentIndices } of sideEffects) {
    const root = disjoint.find(touchedFragmentIndices[0]);
    if (!attachedItemIdsByRoot.has(root)) {
      attachedItemIdsByRoot.set(root, []);
    }
    attachedItemIdsByRoot.get(root).push(sideEffectId);
  }
  return buildVariableDeclarationAtomicUnitsFromGroupedFragments(fragments, disjoint, {
    attachedItemIdsByRoot,
    owner,
  });
}

function touchedFragmentIndicesForSideEffect(sideEffect, { fragmentIndexByMemberName, ownerId }) {
  const touched = new Set();
  let failed = false;
  forEachTopLevelAccess(sideEffect, (access) => {
    if (access.kind !== "local_declaration" || access.ownerId !== ownerId) {
      return true;
    }
    const fragmentIndex = fragmentIndexByMemberName.get(access.name);
    if (fragmentIndex === undefined) {
      failed = true;
      return false;
    }
    touched.add(fragmentIndex);
    return true;
  });
  if (failed) {
    return null;
  }
  return [...touched].sort((left, right) => left - right);
}

function createDisjointSet(size) {
  const parent = Array.from({ length: size }, (_, index) => index);
  const rank = Array.from({ length: size }, () => 0);
  return {
    find(index) {
      if (parent[index] !== index) {
        parent[index] = this.find(parent[index]);
      }
      return parent[index];
    },
    union(left, right) {
      let leftRoot = this.find(left);
      let rightRoot = this.find(right);
      if (leftRoot === rightRoot) {
        return leftRoot;
      }
      if (rank[leftRoot] < rank[rightRoot]) {
        [leftRoot, rightRoot] = [rightRoot, leftRoot];
      }
      parent[rightRoot] = leftRoot;
      if (rank[leftRoot] === rank[rightRoot]) {
        rank[leftRoot] += 1;
      }
      return leftRoot;
    },
    snapshot() {
      return {
        parent: [...parent],
        rank: [...rank],
      };
    },
  };
}

function cloneDisjointSet(disjoint) {
  if (!disjoint?.snapshot) {
    return createDisjointSet(0);
  }
  const snapshot = disjoint.snapshot();
  const parent = [...snapshot.parent];
  const rank = [...snapshot.rank];
  return {
    find(index) {
      if (parent[index] !== index) {
        parent[index] = this.find(parent[index]);
      }
      return parent[index];
    },
    union(left, right) {
      let leftRoot = this.find(left);
      let rightRoot = this.find(right);
      if (leftRoot === rightRoot) {
        return leftRoot;
      }
      if (rank[leftRoot] < rank[rightRoot]) {
        [leftRoot, rightRoot] = [rightRoot, leftRoot];
      }
      parent[rightRoot] = leftRoot;
      if (rank[leftRoot] === rank[rightRoot]) {
        rank[leftRoot] += 1;
      }
      return leftRoot;
    },
    snapshot() {
      return {
        parent: [...parent],
        rank: [...rank],
      };
    },
  };
}

function buildVariableDeclarationAtomicUnitsFromGroupedFragments(
  fragments,
  disjoint,
  { attachedItemIdsByRoot = new Map(), owner }
) {
  const groupsByRoot = new Map();
  fragments.forEach((fragment, fragmentIndex) => {
    const root = disjoint.find(fragmentIndex);
    if (!groupsByRoot.has(root)) {
      groupsByRoot.set(root, {
        fragments: [],
        orderIndex: fragment.orderIndex ?? fragmentIndex,
      });
    }
    const group = groupsByRoot.get(root);
    group.fragments.push(fragment);
    group.orderIndex = Math.min(group.orderIndex, fragment.orderIndex ?? fragmentIndex);
  });
  return [...groupsByRoot.entries()]
    .map(([root, group]) => ({
      attachedItemIds: [...(attachedItemIdsByRoot.get(root) ?? [])],
      ownerFragments: [...group.fragments].sort(
        (left, right) => (left.orderIndex ?? 0) - (right.orderIndex ?? 0) || left.id.localeCompare(right.id)
      ),
      ownerIds: [owner.id],
      orderIndex: group.orderIndex,
    }))
    .sort((left, right) => left.orderIndex - right.orderIndex)
    .map(({ orderIndex, ...unit }) => unit);
}

function buildVariableDeclarationFragmentDependencyDisjointSet(
  owner,
  declarators,
  fragments,
  { analysis = null, programBody = null, statement = null } = {}
) {
  const disjoint = createDisjointSet(fragments.length);
  const fragmentIndexByMemberName = new Map();
  const fragmentIndexByDeclaratorIndex = new Map();
  for (const [fragmentIndex, fragment] of fragments.entries()) {
    for (const memberName of fragment.memberNames) {
      fragmentIndexByMemberName.set(memberName, fragmentIndex);
    }
    for (const declaratorIndex of fragment.declaratorIndices ?? []) {
      fragmentIndexByDeclaratorIndex.set(declaratorIndex, fragmentIndex);
    }
  }
  const sameOwnerBindingNames = new Set(fragments.flatMap((fragment) => fragment.memberNames));
  for (const [declaratorIndex, declarator] of declarators.entries()) {
    const sourceFragmentIndex = fragmentIndexByDeclaratorIndex.get(declaratorIndex);
    if (sourceFragmentIndex === undefined) {
      return null;
    }
    const eagerDependencies = referencedUndeclaredNamesInVariableDeclarator(declarator).filter((name) =>
      sameOwnerBindingNames.has(name)
    );
    for (const dependencyName of eagerDependencies) {
      const targetFragmentIndex = fragmentIndexByMemberName.get(dependencyName);
      if (targetFragmentIndex === undefined) {
        return null;
      }
      disjoint.union(sourceFragmentIndex, targetFragmentIndex);
    }
  }
  if (!ownerHasLazyIntraOwnerBindingWrite(owner)) {
    return disjoint;
  }
  if (!analysis?.owners || !Array.isArray(programBody) || !statement) {
    return null;
  }
  const fragmentAccessContext = {
    owners: analysis.owners,
    runtimeImports: analysis.runtimeImports,
  };
  for (const [fragmentIndex, fragment] of fragments.entries()) {
    const accessRecord = analyzeVariableDeclarationFragmentAccesses(statement, fragment, fragmentAccessContext);
    for (const access of accessRecord.writesTopLevel.lazy) {
      if (access.kind !== "local_declaration" || access.ownerId !== owner.id) {
        continue;
      }
      const targetFragmentIndex = fragmentIndexByMemberName.get(access.name);
      if (targetFragmentIndex === undefined) {
        return null;
      }
      disjoint.union(fragmentIndex, targetFragmentIndex);
    }
  }
  return disjoint;
}

function localDeclarationNamesForAnalysis(analysis) {
  return new Set((analysis?.owners ?? []).flatMap((owner) => owner.names ?? []));
}

function isStaticallyPureFragmentInitializer(node) {
  if (!node) {
    return true;
  }
  if (
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
    return node.expressions.length === 0;
  }
  if (t.isArrayExpression(node)) {
    return node.elements.every((element) => {
      if (!element) {
        return true;
      }
      if (t.isSpreadElement(element)) {
        return false;
      }
      return isStaticallyPureFragmentInitializer(element);
    });
  }
  if (t.isObjectExpression(node)) {
    return node.properties.every((property) => {
      if (t.isSpreadElement(property)) {
        return false;
      }
      if (property.computed && !isStaticallyPureFragmentInitializer(property.key)) {
        return false;
      }
      return isStaticallyPureFragmentInitializer(property.value);
    });
  }
  if (t.isUnaryExpression(node)) {
    return isStaticallyPureFragmentInitializer(node.argument);
  }
  if (t.isBinaryExpression(node) || t.isLogicalExpression(node)) {
    return isStaticallyPureFragmentInitializer(node.left) && isStaticallyPureFragmentInitializer(node.right);
  }
  if (t.isConditionalExpression(node)) {
    return (
      isStaticallyPureFragmentInitializer(node.test) &&
      isStaticallyPureFragmentInitializer(node.consequent) &&
      isStaticallyPureFragmentInitializer(node.alternate)
    );
  }
  if (t.isParenthesizedExpression(node)) {
    return isStaticallyPureFragmentInitializer(node.expression);
  }
  return false;
}

function isLocallyPureFragmentInitializer(node, localDeclarationNames) {
  if (!node || !(localDeclarationNames instanceof Set)) {
    return false;
  }
  if (isStaticallyPureFragmentInitializer(node)) {
    return true;
  }
  if (t.isIdentifier(node)) {
    return localDeclarationNames.has(node.name);
  }
  if (t.isTemplateLiteral(node)) {
    return node.expressions.every((expression) => isLocallyPureFragmentInitializer(expression, localDeclarationNames));
  }
  if (t.isArrayExpression(node)) {
    return node.elements.every((element) => {
      if (!element) {
        return true;
      }
      if (t.isSpreadElement(element)) {
        return false;
      }
      return isLocallyPureFragmentInitializer(element, localDeclarationNames);
    });
  }
  if (t.isObjectExpression(node)) {
    return node.properties.every((property) => {
      if (t.isSpreadElement(property)) {
        return false;
      }
      if (property.computed && !isLocallyPureFragmentInitializer(property.key, localDeclarationNames)) {
        return false;
      }
      return isLocallyPureFragmentInitializer(property.value, localDeclarationNames);
    });
  }
  if (t.isUnaryExpression(node)) {
    return isLocallyPureFragmentInitializer(node.argument, localDeclarationNames);
  }
  if (t.isBinaryExpression(node) || t.isLogicalExpression(node)) {
    return (
      isLocallyPureFragmentInitializer(node.left, localDeclarationNames) &&
      isLocallyPureFragmentInitializer(node.right, localDeclarationNames)
    );
  }
  if (t.isConditionalExpression(node)) {
    return (
      isLocallyPureFragmentInitializer(node.test, localDeclarationNames) &&
      isLocallyPureFragmentInitializer(node.consequent, localDeclarationNames) &&
      isLocallyPureFragmentInitializer(node.alternate, localDeclarationNames)
    );
  }
  if (t.isParenthesizedExpression(node)) {
    return isLocallyPureFragmentInitializer(node.expression, localDeclarationNames);
  }
  return false;
}

function isLazyCallableFragmentInitializer(node) {
  return t.isFunctionExpression(node) || t.isArrowFunctionExpression(node);
}

function isLazyClassFragmentInitializer(node) {
  if (!t.isClassExpression(node) || node.superClass || (node.decorators?.length ?? 0) > 0) {
    return false;
  }
  return node.body.body.every((member) => {
    if (t.isClassMethod(member) || t.isClassPrivateMethod(member) || t.isTSDeclareMethod(member)) {
      return !member.computed && (member.decorators?.length ?? 0) === 0;
    }
    return false;
  });
}

function bindingNamesForVariableDeclarator(declarator) {
  if (!declarator?.id) {
    return [];
  }
  if (t.isIdentifier(declarator.id)) {
    return [declarator.id.name];
  }
  if (t.isObjectPattern(declarator.id)) {
    return declarator.id.properties.flatMap((property) => {
      if (t.isRestElement(property)) {
        return bindingNamesForPattern(property.argument);
      }
      return bindingNamesForPattern(property.value);
    });
  }
  if (t.isArrayPattern(declarator.id)) {
    return declarator.id.elements.flatMap((element) => bindingNamesForPattern(element));
  }
  return bindingNamesForPattern(declarator.id);
}

function bindingNamesForPattern(pattern) {
  if (!pattern) {
    return [];
  }
  if (t.isIdentifier(pattern)) {
    return [pattern.name];
  }
  if (t.isRestElement(pattern)) {
    return bindingNamesForPattern(pattern.argument);
  }
  if (t.isAssignmentPattern(pattern)) {
    return bindingNamesForPattern(pattern.left);
  }
  if (t.isObjectPattern(pattern)) {
    return pattern.properties.flatMap((property) => {
      if (t.isRestElement(property)) {
        return bindingNamesForPattern(property.argument);
      }
      return bindingNamesForPattern(property.value);
    });
  }
  if (t.isArrayPattern(pattern)) {
    return pattern.elements.flatMap((element) => bindingNamesForPattern(element));
  }
  return [];
}

function ownerHasAnyTopLevelAccess(owner) {
  let hasAccess = false;
  forEachTopLevelAccess(owner, () => {
    hasAccess = true;
    return false;
  });
  return hasAccess;
}

function ownerHasAnyEagerTopLevelAccess(owner) {
  return (
    selectedModuleEagerReadAccesses(owner).length > 0 ||
    selectedModuleWriteAccesses(owner).length > 0 ||
    selectedModuleEagerMemberWriteAccesses(owner).length > 0
  );
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
  if (Array.isArray(unit.ownerFragments) && unit.ownerFragments.length > 0) {
    for (const fragment of unit.ownerFragments) {
      const metrics = statementMetricForOwnerFragment(fragment, { code, itemById, programBody });
      lines += metrics.lines;
      if (bytes !== null) {
        bytes += metrics.bytes;
      }
      const baseOrdinal = itemById.get(fragment.ownerId)?.ordinal ?? Number.POSITIVE_INFINITY;
      const fragmentOrdinal = baseOrdinal + fragment.orderIndex / 1000;
      if (fragmentOrdinal < startOrdinal) {
        startOrdinal = fragmentOrdinal;
      }
    }
  } else {
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
  }
  return {
    attachedItemIds: [...unit.attachedItemIds],
    bytes,
    id,
    index,
    lines,
    memberNames:
      Array.isArray(unit.ownerFragments) && unit.ownerFragments.length > 0
        ? unit.ownerFragments.flatMap((fragment) => fragment.memberNames).sort()
        : unit.ownerIds.flatMap((ownerId) => ownerById.get(ownerId)?.names ?? []).sort(),
    ownerIds: [...unit.ownerIds],
    ownerFragments: cloneOwnerFragments(unit.ownerFragments),
    startOrdinal,
  };
}

function statementMetricForOwnerFragment(fragment, { code, itemById, programBody }) {
  const item = itemById.get(fragment.ownerId);
  const statement = unwrapTopLevelDeclarationNode(programBody?.[item?.ordinal]);
  if (!t.isVariableDeclaration(statement)) {
    return { bytes: 0, lines: 0 };
  }
  const lines = fragment.declaratorIndices.reduce((sum, declaratorIndex) => {
    const declaration = statement.declarations[declaratorIndex];
    if (!declaration?.loc) {
      return sum;
    }
    return sum + declaration.loc.end.line - declaration.loc.start.line + 1;
  }, 0);
  const bytes =
    typeof code === "string"
      ? fragment.declaratorIndices.reduce((sum, declaratorIndex) => {
          const declaration = statement.declarations[declaratorIndex];
          if (typeof declaration?.start !== "number" || typeof declaration?.end !== "number") {
            return sum;
          }
          return sum + Buffer.byteLength(code.slice(declaration.start, declaration.end));
        }, 0)
      : 0;
  return { bytes, lines };
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

function newModuleFromAtomicUnit(atomicUnit) {
  return {
    attachedItemIds: [...atomicUnit.attachedItemIds],
    bytes: atomicUnit.bytes,
    lines: atomicUnit.lines,
    memberNames: [...atomicUnit.memberNames],
    ownerIds: [...atomicUnit.ownerIds],
    ownerFragments: cloneOwnerFragments(atomicUnit.ownerFragments),
    startOrdinal: atomicUnit.startOrdinal,
    unitIds: [atomicUnit.id],
  };
}

function finalizeModulePlan(modulePlan, { id, index, ownerById }) {
  const uniqueMemberNames = [...new Set(modulePlan.memberNames)].sort();
  const nameHint = moduleNameHint(uniqueMemberNames, index);
  const modulePath = normalizeRelativeFile(modulePlan.modulePath ?? sanitizeIdentifier(`${id}__${nameHint}`));
  return {
    attachedItemIds: [...new Set(modulePlan.attachedItemIds)].sort(),
    bytes: modulePlan.bytes,
    id,
    index,
    lines: modulePlan.lines,
    memberNames: uniqueMemberNames,
    modulePath,
    nameHint,
    ownerIds: [...new Set(modulePlan.ownerIds)].sort(
      (leftOwnerId, rightOwnerId) => ownerById.get(leftOwnerId).ordinal - ownerById.get(rightOwnerId).ordinal
    ),
    ownerFragments: cloneOwnerFragments(modulePlan.ownerFragments),
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
  if (typeof code !== "string") {
    return 0;
  }
  const item = itemById.get(itemId);
  const statement = programBody[item?.ordinal];
  if (typeof statement?.start !== "number" || typeof statement?.end !== "number") {
    return 0;
  }
  return Buffer.byteLength(code.slice(statement.start, statement.end));
}

function selectedModuleEagerReadAccesses(record) {
  return topLevelAccesses(record, "reads", "eager");
}

function selectedModuleLazyReadAccesses(record) {
  return topLevelAccesses(record, "reads", "lazy");
}

function selectedModuleWriteAccesses(record) {
  return topLevelAccesses(record, "writes", "eager");
}

function selectedModuleLazyWriteAccesses(record) {
  return topLevelAccesses(record, "writes", "lazy");
}

function selectedModuleEagerMemberWriteAccesses(record) {
  return topLevelAccesses(record, "memberWrites", "eager");
}

function selectedModuleLazyMemberWriteAccesses(record) {
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
  for (const access of selectedModuleEagerReadAccesses(record)) {
    if (callback(access, "reads", "eager") === false) {
      return false;
    }
  }
  for (const access of selectedModuleLazyReadAccesses(record)) {
    if (callback(access, "reads", "lazy") === false) {
      return false;
    }
  }
  for (const access of selectedModuleWriteAccesses(record)) {
    if (callback(access, "writes", "eager") === false) {
      return false;
    }
  }
  for (const access of selectedModuleLazyWriteAccesses(record)) {
    if (callback(access, "writes", "lazy") === false) {
      return false;
    }
  }
  for (const access of selectedModuleEagerMemberWriteAccesses(record)) {
    if (callback(access, "memberWrites", "eager") === false) {
      return false;
    }
  }
  for (const access of selectedModuleLazyMemberWriteAccesses(record)) {
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

function normalizeOptionalRelativeDir(value) {
  if (value === null || value === undefined) {
    return null;
  }
  return normalizeRelativeFile(value);
}

function sanitizeIdentifier(value) {
  return value.replace(/[^A-Za-z0-9_$]+/g, "_").replace(/^[^A-Za-z_$]+/, "_");
}

function unwrapTopLevelDeclarationNode(node) {
  if (t.isExportNamedDeclaration(node) && node.declaration) {
    return node.declaration;
  }
  return node;
}

function cloneOwnerFragments(ownerFragments) {
  return Array.isArray(ownerFragments)
    ? ownerFragments.map((fragment) => ({
        ...fragment,
        declaratorIndices: [...fragment.declaratorIndices],
        memberNames: [...fragment.memberNames],
      }))
    : undefined;
}

function durationMsSince(startedAt) {
  return Number(process.hrtime.bigint() - startedAt) / 1_000_000;
}
