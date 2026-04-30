import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { parse } from "@babel/parser";
import traverseModule from "@babel/traverse";
import * as t from "@babel/types";
import { DEFAULT_PARSER_OPTIONS, writeJsonFile } from "../common/parser_options.mjs";
import {
  getArtifactManifestChunks,
  getChunkEntryFile,
  getChunkEntryPath,
  requirePipelineArtifact,
} from "../common/artifact.mjs";
import {
  formatDurationSince,
  logProgress,
  prepareOutputDir,
  relativeWorkspacePath,
  resolveWorkspacePath,
} from "../common/io.mjs";
import {
  referencedUndeclaredNames,
  referencedUndeclaredNamesInVariableDeclarator,
} from "../common/program_analysis.mjs";

const traverse = traverseModule.default ?? traverseModule;

const RUNTIME_SENSITIVE_EFFECTS = new Set(["containsDirectEval", "containsImportMeta", "containsTopLevelAwait"]);
const SELECTED_MODULE_SUPPORTED_OWNER_TYPES = new Set([
  "FunctionDeclaration",
  "ClassDeclaration",
  "VariableDeclaration",
]);
const FRAGMENT_IMPORT_BY_LOCAL_CACHE = new WeakMap();
const FRAGMENT_OWNER_BY_BINDING_CACHE = new WeakMap();

export function extractRuntimeBoundaryMetadata(options) {
  const artifact = requirePipelineArtifact(options.artifact, "extractRuntimeBoundaryMetadata");
  const inputRoot = options.inputRoot ? resolveWorkspacePath(options.inputRoot) : null;
  const outDir = resolveWorkspacePath(options.outDir);
  const inputManifestPath = options.inputManifestPath ? resolveWorkspacePath(options.inputManifestPath) : null;
  const summaryPath = resolveWorkspacePath(options.summaryPath ?? join(dirname(outDir), "boundary-summary.json"));

  prepareOutputDir(outDir, { force: options.force });
  const selectedChunks = selectBoundaryChunks(getArtifactManifestChunks(artifact), options.chunkIds);

  logProgress(
    `boundary-analysis start chunks=${selectedChunks.length} mode=pipeline out=${relativeWorkspacePath(outDir)}`
  );
  const startedAt = process.hrtime.bigint();

  const chunkSummaries = [];
  for (const chunk of selectedChunks) {
    const entryFile = getChunkEntryFile(artifact, chunk.chunkId);
    const entryRelativePath = getChunkEntryPath(artifact, chunk.chunkId);
    if (!entryFile?.ast || !entryRelativePath) {
      throw new Error(`extractRuntimeBoundaryMetadata missing entry AST for chunk: ${chunk.chunkId}`);
    }
    const analysis = analyzeRuntimeBoundaryAst(entryFile.ast, {
      chunkId: chunk.chunkId,
      manifestPath: describeArtifactChunkPath(inputRoot, chunk.chunkId, "manifest.json"),
      runtimePath: describeArtifactChunkPath(inputRoot, chunk.chunkId, entryRelativePath),
    });
    writeChunkAnalysis(outDir, analysis);
    chunkSummaries.push(chunkSummaryFromAnalysis(analysis));
  }

  const summary = buildBoundarySummary({
    chunkSummaries,
    inputRoot,
    outDir,
    inputManifestPath,
  });
  writeJsonFile(summaryPath, summary);
  logProgress(
    `boundary-analysis done chunks=${chunkSummaries.length} duration=${formatDurationSince(
      startedAt
    )} summary=${relativeWorkspacePath(summaryPath)}`
  );

  return {
    artifact,
    manifest: summary,
  };
}

function writeChunkAnalysis(outDir, analysis) {
  const outputPath = join(outDir, `${analysis.chunkId}.json`);
  mkdirSync(dirname(outputPath), { recursive: true });
  writeJsonFile(outputPath, analysis);
}

function chunkSummaryFromAnalysis(analysis) {
  return {
    chunkId: analysis.chunkId,
    counts: analysis.counts,
    runtimePath: analysis.runtimePath,
  };
}

function selectBoundaryChunks(chunks, selectedChunkIds) {
  if (!selectedChunkIds || selectedChunkIds.length === 0) {
    return chunks;
  }
  const selected = new Set(selectedChunkIds);
  const filtered = chunks.filter((chunk) => selected.has(chunk.chunkId));
  if (filtered.length !== selected.size) {
    const missing = [...selected].filter((chunkId) => !filtered.some((chunk) => chunk.chunkId === chunkId));
    throw new Error(`Boundary analysis missing chunks: ${missing.sort().join(", ")}`);
  }
  return filtered;
}

export function analyzeRuntimeBoundaryCode(code, options = {}) {
  const ast = parse(code, options.parser ?? DEFAULT_PARSER_OPTIONS);
  return analyzeRuntimeBoundaryAst(ast, options);
}

export function analyzeRuntimeBoundaryAst(ast, { chunkId = "<chunk>", manifestPath = null, runtimePath = null } = {}) {
  const imports = [];
  const importByLocalName = new Map();
  const owners = [];
  const sideEffects = [];
  const ownerByBinding = new Map();
  const itemByTopNode = new Map();
  const programItems = [];
  const programPathByNode = new WeakMap();

  // Single-traverse analysis: classify the program body inside the
  // Program enter visitor (eliminating the previous separate traverse
  // just to find programPath). Subsequent expression visitors descend
  // through the same traverse and use the already-populated maps.
  traverse(ast, {
    Program: {
      enter(programPath) {
        programPathByNode.set(programPath.node, programPath);
        for (const topPath of programPath.get("body")) {
          const node = topPath.node;
          programPathByNode.set(node, topPath);
          if (topPath.isImportDeclaration()) {
            const importRecord = describeImport(node, imports.length);
            imports.push(importRecord);
            itemByTopNode.set(node, importRecord);
            programItems.push({
              id: importRecord.id,
              kind: "import",
              line: importRecord.line,
              names: importRecord.specifiers.map((specifier) => specifier.local).sort(),
              ordinal: topPath.key,
              source: importRecord.source,
              type: node.type,
            });
            for (const specifier of importRecord.specifiers) {
              importByLocalName.set(specifier.local, {
                ...specifier,
                id: importRecord.id,
                source: importRecord.source,
              });
            }
            continue;
          }

          const names = topLevelDeclarationNames(node);
          if (names.length > 0) {
            const owner = createOwnerRecord({
              id: `owner_${owners.length.toString().padStart(5, "0")}`,
              line: node.loc?.start.line ?? null,
              names,
              node,
              ordinal: topPath.key,
              path: topPath,
              type: node.type,
            });
            owner.classFeatures =
              owner.type === "ClassDeclaration" ? describeClassFeatures(owner.node) : emptyClassFeatures();
            owners.push(owner);
            itemByTopNode.set(node, owner);
            programItems.push({
              id: owner.id,
              kind: "declaration",
              line: owner.line,
              names: owner.names,
              ordinal: owner.ordinal,
              type: owner.type,
            });
            for (const name of names) {
              ownerByBinding.set(name, owner);
            }
            continue;
          }

          const sideEffect = createSideEffectRecord({
            id: `side_effect_${sideEffects.length.toString().padStart(5, "0")}`,
            line: node.loc?.start.line ?? null,
            node,
            ordinal: topPath.key,
            path: topPath,
            type: node.type,
          });
          sideEffects.push(sideEffect);
          itemByTopNode.set(node, sideEffect);
          programItems.push({
            id: sideEffect.id,
            kind: "side_effect",
            line: sideEffect.line,
            names: [],
            ordinal: sideEffect.ordinal,
            type: sideEffect.type,
          });
        }
        programPath.scope.crawl();
      },
    },
    AssignmentExpression(path) {
      if (!isInsideTrackedProgramItem(path, itemByTopNode)) {
        return;
      }
      recordAssignmentTargets(path, path.node.left, "binding_write", {
        importByLocalName,
        itemByTopNode,
        ownerByBinding,
        programPathByNode,
      });
    },
    AwaitExpression(path) {
      recordEffectOccurrence(path, "containsTopLevelAwait", { itemByTopNode, programPathByNode });
    },
    CallExpression(path) {
      if (t.isIdentifier(path.node.callee) && path.node.callee.name === "eval" && !path.scope.getBinding("eval")) {
        recordEffectOccurrence(path.get("callee"), "containsDirectEval", { itemByTopNode, programPathByNode });
      }
      if (path.node.callee.type === "Import") {
        recordEffectOccurrence(path, "dynamicImport", { itemByTopNode, programPathByNode });
        if (t.isStringLiteral(path.node.arguments?.[0])) {
          recordEffectOccurrence(path, "containsRuntimeSourceRebase", { itemByTopNode, programPathByNode });
        }
      }
    },
    NewExpression(path) {
      if (!t.isIdentifier(path.node.callee)) {
        return;
      }
      if (path.scope.getBinding(path.node.callee.name)) {
        return;
      }
      if (
        (path.node.callee.name === "Worker" || path.node.callee.name === "SharedWorker") &&
        t.isStringLiteral(path.node.arguments?.[0])
      ) {
        recordEffectOccurrence(path, "containsRuntimeSourceRebase", { itemByTopNode, programPathByNode });
      }
    },
    ForInStatement(path) {
      if (!isInsideTrackedProgramItem(path, itemByTopNode)) {
        return;
      }
      if (path.node.left?.type === "VariableDeclaration") {
        return;
      }
      recordAssignmentTargets(path, path.node.left, "binding_write", {
        importByLocalName,
        itemByTopNode,
        ownerByBinding,
        programPathByNode,
      });
    },
    ForOfStatement(path) {
      if (!isInsideTrackedProgramItem(path, itemByTopNode)) {
        return;
      }
      if (path.node.left?.type === "VariableDeclaration") {
        return;
      }
      recordAssignmentTargets(path, path.node.left, "binding_write", {
        importByLocalName,
        itemByTopNode,
        ownerByBinding,
        programPathByNode,
      });
    },
    ImportExpression(path) {
      recordEffectOccurrence(path, "dynamicImport", { itemByTopNode, programPathByNode });
    },
    MetaProperty(path) {
      if (path.node.meta.name === "import" && path.node.property.name === "meta") {
        if (isSafeRootRelativeImportMetaUrlUse(path)) {
          return;
        }
        recordEffectOccurrence(path, "containsImportMeta", { itemByTopNode, programPathByNode });
      }
    },
    ReferencedIdentifier(path) {
      recordTopLevelRead(path, {
        importByLocalName,
        itemByTopNode,
        ownerByBinding,
        programPathByNode,
      });
    },
    UnaryExpression(path) {
      if (path.node.operator !== "delete") {
        return;
      }
      if (!t.isMemberExpression(path.node.argument)) {
        return;
      }
      recordAssignmentTargets(path, path.node.argument, "member_write", {
        importByLocalName,
        itemByTopNode,
        ownerByBinding,
        programPathByNode,
      });
    },
    UpdateExpression(path) {
      if (!isInsideTrackedProgramItem(path, itemByTopNode)) {
        return;
      }
      recordAssignmentTargets(path, path.node.argument, "binding_write", {
        importByLocalName,
        itemByTopNode,
        ownerByBinding,
        programPathByNode,
      });
    },
  });

  finalizeOwnerExtractorCompatibility(owners, sideEffects);
  finalizeOwnerModes(owners);
  const localReferenceEdges = buildLocalReferenceEdges(owners);
  const mutationEdges = buildMutationEdges(owners);
  const interactionEdges = buildInteractionEdges(localReferenceEdges, mutationEdges);
  const eagerInitEdges = buildEagerInitEdges(localReferenceEdges, mutationEdges);
  const interactionSccs = buildOwnerComponents(owners, interactionEdges, "interaction_scc");
  const weakInteractionComponents = buildWeakOwnerComponents(owners, interactionEdges, "weak_interaction_component");
  const eagerInitSccs = buildOwnerComponents(owners, eagerInitEdges, "eager_init_scc");
  const eagerInitComponentIndex = indexComponentsByOwnerId(eagerInitSccs);
  const interactionComponentIndex = indexComponentsByOwnerId(interactionSccs);
  const eagerInitDag = buildComponentDag(eagerInitSccs, eagerInitEdges);

  for (const owner of owners) {
    owner.eagerInitComponentId = eagerInitComponentIndex.get(owner.id) ?? null;
    owner.interactionComponentId = interactionComponentIndex.get(owner.id) ?? null;
  }

  const ownerOutput = owners.map((owner) => finalizeOwnerRecord(owner));
  const sideEffectOutput = sideEffects.map((sideEffect) => finalizeSideEffectRecord(sideEffect));
  const programItemOutput = programItems.map((item) => finalizeProgramItem(item, ownerOutput, sideEffectOutput));
  const selectedModuleRegions = buildSelectedModuleRegions({
    weakInteractionComponents,
    owners,
    sideEffects,
    programItems,
  });
  const counts = summarizeBoundaryCounts({
    eagerInitDag,
    eagerInitEdges,
    eagerInitSccs,
    imports,
    interactionEdges,
    interactionSccs,
    selectedModuleRegions,
    ownerOutput,
    programItemOutput,
    sideEffectOutput,
  });

  return {
    schemaVersion: 1,
    kind: "js.runtime_boundary_analysis",
    chunkId,
    runtimePath,
    manifestPath,
    counts,
    runtimeImports: imports,
    programItems: programItemOutput,
    owners: ownerOutput,
    sideEffects: sideEffectOutput,
    graphs: {
      eagerInit: eagerInitEdges,
      interaction: interactionEdges,
      mutation: mutationEdges,
      localReferences: localReferenceEdges,
    },
    eagerInitComponents: eagerInitSccs,
    interactionComponents: interactionSccs,
    selectedModuleRegions,
    eagerInitComponentDag: eagerInitDag,
  };
}

export function analyzeVariableDeclarationFragmentAccesses(
  statementNode,
  fragment,
  { owners = [], runtimeImports = [] } = {}
) {
  const statement = unwrapTopLevelDeclarationNode(statementNode);
  if (!t.isVariableDeclaration(statement)) {
    throw new Error(
      `analyzeVariableDeclarationFragmentAccesses expected VariableDeclaration, got ${statement?.type ?? "unknown"}`
    );
  }
  const declaratorIndices = [...new Set(fragment?.declaratorIndices ?? [])].sort((left, right) => left - right);
  if (declaratorIndices.length === 0) {
    return {
      readsTopLevel: { eager: [], lazy: [] },
      writesTopLevel: { eager: [], lazy: [] },
      memberWritesTopLevel: { eager: [], lazy: [] },
    };
  }

  const selectedDeclarators = [];
  for (const declaratorIndex of declaratorIndices) {
    const declarator = statement.declarations[declaratorIndex];
    if (!declarator) {
      throw new Error(
        `Fragment analysis missing declarator ${declaratorIndex} for ${fragment.ownerId ?? "owner_fragment"}`
      );
    }
    selectedDeclarators.push(t.cloneNode(declarator, true));
  }

  const importByLocalName = fragmentImportByLocalName(runtimeImports);
  const ownerByBinding = fragmentOwnerByBinding(owners);
  const fragmentStatement = t.variableDeclaration(statement.kind, selectedDeclarators);
  const fragmentAst = t.file(t.program([fragmentStatement]));
  const itemByTopNode = new Map();
  const programPathByNode = new WeakMap();
  let fragmentRecord = null;
  let selectedDeclaratorNodes = null;

  traverse(fragmentAst, {
    Program: {
      enter(programPath) {
        programPathByNode.set(programPath.node, programPath);
        const [topPath] = programPath.get("body");
        if (!topPath?.isVariableDeclaration()) {
          throw new Error("Fragment analysis expected a top-level variable declaration");
        }
        programPathByNode.set(topPath.node, topPath);
        fragmentRecord = createOwnerRecord({
          id: fragment.ownerId ?? "owner_fragment",
          line: topPath.node.loc?.start.line ?? null,
          names: [...(fragment.memberNames ?? [])],
          node: topPath.node,
          ordinal: 0,
          path: topPath,
          type: topPath.node.type,
        });
        itemByTopNode.set(topPath.node, fragmentRecord);
        selectedDeclaratorNodes = new Set(topPath.node.declarations);
        programPath.scope.crawl();
      },
    },
    AssignmentExpression(path) {
      if (!isInsideSelectedVariableDeclarator(path, itemByTopNode, selectedDeclaratorNodes)) {
        return;
      }
      recordAssignmentTargets(path, path.node.left, "binding_write", {
        importByLocalName,
        itemByTopNode,
        ownerByBinding,
        programPathByNode,
      });
    },
    ForInStatement(path) {
      if (!isInsideSelectedVariableDeclarator(path, itemByTopNode, selectedDeclaratorNodes)) {
        return;
      }
      if (path.node.left?.type === "VariableDeclaration") {
        return;
      }
      recordAssignmentTargets(path, path.node.left, "binding_write", {
        importByLocalName,
        itemByTopNode,
        ownerByBinding,
        programPathByNode,
      });
    },
    ForOfStatement(path) {
      if (!isInsideSelectedVariableDeclarator(path, itemByTopNode, selectedDeclaratorNodes)) {
        return;
      }
      if (path.node.left?.type === "VariableDeclaration") {
        return;
      }
      recordAssignmentTargets(path, path.node.left, "binding_write", {
        importByLocalName,
        itemByTopNode,
        ownerByBinding,
        programPathByNode,
      });
    },
    ReferencedIdentifier(path) {
      if (!isInsideSelectedVariableDeclarator(path, itemByTopNode, selectedDeclaratorNodes)) {
        return;
      }
      recordTopLevelRead(path, {
        importByLocalName,
        itemByTopNode,
        ownerByBinding,
        programPathByNode,
      });
    },
    UnaryExpression(path) {
      if (path.node.operator !== "delete") {
        return;
      }
      if (!t.isMemberExpression(path.node.argument)) {
        return;
      }
      if (!isInsideSelectedVariableDeclarator(path, itemByTopNode, selectedDeclaratorNodes)) {
        return;
      }
      recordAssignmentTargets(path, path.node.argument, "member_write", {
        importByLocalName,
        itemByTopNode,
        ownerByBinding,
        programPathByNode,
      });
    },
    UpdateExpression(path) {
      if (!isInsideSelectedVariableDeclarator(path, itemByTopNode, selectedDeclaratorNodes)) {
        return;
      }
      recordAssignmentTargets(path, path.node.argument, "binding_write", {
        importByLocalName,
        itemByTopNode,
        ownerByBinding,
        programPathByNode,
      });
    },
  });

  return {
    readsTopLevel: finalizeAccessBuckets(fragmentRecord.eagerReads, fragmentRecord.lazyReads),
    writesTopLevel: finalizeAccessBuckets(fragmentRecord.eagerWrites, fragmentRecord.lazyWrites),
    memberWritesTopLevel: finalizeAccessBuckets(fragmentRecord.eagerMemberWrites, fragmentRecord.lazyMemberWrites),
  };
}

function fragmentImportByLocalName(runtimeImports) {
  const cached = FRAGMENT_IMPORT_BY_LOCAL_CACHE.get(runtimeImports);
  if (cached) {
    return cached;
  }
  const importByLocalName = new Map();
  for (const importRecord of runtimeImports) {
    for (const specifier of importRecord.specifiers ?? []) {
      importByLocalName.set(specifier.local, {
        ...specifier,
        id: importRecord.id,
        source: importRecord.source,
      });
    }
  }
  FRAGMENT_IMPORT_BY_LOCAL_CACHE.set(runtimeImports, importByLocalName);
  return importByLocalName;
}

function fragmentOwnerByBinding(owners) {
  const cached = FRAGMENT_OWNER_BY_BINDING_CACHE.get(owners);
  if (cached) {
    return cached;
  }
  const ownerByBinding = new Map();
  for (const owner of owners) {
    for (const name of owner.names ?? []) {
      ownerByBinding.set(name, owner);
    }
  }
  FRAGMENT_OWNER_BY_BINDING_CACHE.set(owners, ownerByBinding);
  return ownerByBinding;
}

function createOwnerRecord({ id, line, names, node, ordinal, path, type }) {
  return {
    id,
    kind: "declaration",
    line,
    names: [...names].sort(),
    node,
    ordinal,
    path,
    type,
    classFeatures: emptyClassFeatures(),
    effects: {
      containsDirectEval: false,
      containsImportMeta: false,
      containsRuntimeSourceRebase: false,
      containsTopLevelAwait: false,
      eagerDynamicImportCount: 0,
      lazyDynamicImportCount: 0,
    },
    eagerReads: new Map(),
    lazyReads: new Map(),
    eagerWrites: new Map(),
    lazyWrites: new Map(),
    eagerMemberWrites: new Map(),
    lazyMemberWrites: new Map(),
    extractionMode: "keep_runtime",
    extractionReasons: [],
    currentExtractorBlockingReasons: [],
    currentExtractorCompatible: true,
    currentExtractorLowering: "standard",
    eagerInitComponentId: null,
    interactionComponentId: null,
  };
}

function createSideEffectRecord({ id, line, node, ordinal, path, type }) {
  return {
    id,
    kind: "side_effect",
    line,
    node,
    ordinal,
    path,
    type,
    effects: {
      containsDirectEval: false,
      containsImportMeta: false,
      containsRuntimeSourceRebase: false,
      containsTopLevelAwait: false,
      eagerDynamicImportCount: 0,
      lazyDynamicImportCount: 0,
    },
    eagerReads: new Map(),
    lazyReads: new Map(),
    eagerWrites: new Map(),
    lazyWrites: new Map(),
    eagerMemberWrites: new Map(),
    lazyMemberWrites: new Map(),
    extractionMode: "keep_runtime",
    extractionReasons: ["top_level_side_effect_statement"],
  };
}

function emptyClassFeatures() {
  return {
    computedKeyCount: 0,
    hasSuperClass: false,
    instanceFieldCount: 0,
    staticBlockCount: 0,
    staticFieldCount: 0,
  };
}

function describeImport(node, index) {
  return {
    id: `import_${index.toString().padStart(5, "0")}`,
    kind: "import",
    line: node.loc?.start.line ?? null,
    source: node.source.value,
    specifiers: node.specifiers.map((specifier) => {
      if (specifier.type === "ImportDefaultSpecifier") {
        return {
          kind: "default",
          local: specifier.local.name,
        };
      }
      if (specifier.type === "ImportNamespaceSpecifier") {
        return {
          kind: "namespace",
          local: specifier.local.name,
        };
      }
      return {
        imported: specifierName(specifier.imported),
        kind: "named",
        local: specifier.local.name,
      };
    }),
  };
}

function specifierName(node) {
  if (!node) {
    return null;
  }
  if (node.type === "Identifier") {
    return node.name;
  }
  return node.value;
}

function topLevelDeclarationNames(node) {
  if (node.type === "FunctionDeclaration" || node.type === "ClassDeclaration") {
    return node.id ? [node.id.name] : [];
  }
  if (node.type === "VariableDeclaration") {
    return node.declarations.flatMap((declaration) => bindingNames(declaration.id));
  }
  if (node.type === "ExportNamedDeclaration" && node.declaration) {
    return topLevelDeclarationNames(node.declaration);
  }
  return [];
}

function bindingNames(node) {
  if (!node) {
    return [];
  }
  if (node.type === "Identifier") {
    return [node.name];
  }
  if (node.type === "RestElement") {
    return bindingNames(node.argument);
  }
  if (node.type === "AssignmentPattern") {
    return bindingNames(node.left);
  }
  if (node.type === "ArrayPattern") {
    return node.elements.flatMap((element) => bindingNames(element));
  }
  if (node.type === "ObjectPattern") {
    return node.properties.flatMap((property) => {
      if (property.type === "RestElement") {
        return bindingNames(property.argument);
      }
      return bindingNames(property.value);
    });
  }
  return [];
}

function assignmentTargetDescriptors(node) {
  if (!node) {
    return [];
  }
  if (node.type === "Identifier") {
    return [{ kind: "binding", name: node.name }];
  }
  if (node.type === "RestElement") {
    return assignmentTargetDescriptors(node.argument);
  }
  if (node.type === "AssignmentPattern") {
    return assignmentTargetDescriptors(node.left);
  }
  if (node.type === "ArrayPattern") {
    return node.elements.flatMap((element) => assignmentTargetDescriptors(element));
  }
  if (node.type === "ObjectPattern") {
    return node.properties.flatMap((property) => {
      if (property.type === "RestElement") {
        return assignmentTargetDescriptors(property.argument);
      }
      return assignmentTargetDescriptors(property.value);
    });
  }
  if (node.type === "MemberExpression") {
    const base = memberExpressionBaseIdentifier(node);
    return base ? [{ kind: "member", name: base.name }] : [];
  }
  if (
    node.type === "TSAsExpression" ||
    node.type === "TSTypeAssertion" ||
    node.type === "TSNonNullExpression" ||
    node.type === "ParenthesizedExpression"
  ) {
    return assignmentTargetDescriptors(node.expression);
  }
  return [];
}

function memberExpressionBaseIdentifier(node) {
  let current = node;
  while (t.isMemberExpression(current)) {
    current = current.object;
  }
  if (
    t.isTSAsExpression(current) ||
    t.isTSTypeAssertion(current) ||
    t.isTSNonNullExpression(current) ||
    t.isParenthesizedExpression(current)
  ) {
    return memberExpressionBaseIdentifier(current.expression);
  }
  return t.isIdentifier(current) ? current : null;
}

function isInsideTrackedProgramItem(path, itemByTopNode) {
  const topPath = topLevelProgramChild(path);
  return Boolean(topPath && itemByTopNode.has(topPath.node));
}

function isInsideSelectedVariableDeclarator(path, itemByTopNode, selectedDeclaratorNodes) {
  if (!selectedDeclaratorNodes || !isInsideTrackedProgramItem(path, itemByTopNode)) {
    return false;
  }
  let current = path;
  while (current && !current.isProgram?.()) {
    if (current.isVariableDeclarator?.() && selectedDeclaratorNodes.has(current.node)) {
      return true;
    }
    current = current.parentPath;
  }
  return false;
}

function topLevelProgramChild(path) {
  let current = path;
  while (current.parentPath && !current.parentPath.isProgram()) {
    current = current.parentPath;
  }
  return current.parentPath?.isProgram() ? current : null;
}

function recordTopLevelRead(path, context) {
  const resolution = resolveTopLevelBinding(path, path.node.name, context);
  if (!resolution) {
    return;
  }
  const sourceItem = sourceItemForPath(path, context);
  if (!sourceItem || sourceItem.kind === "import") {
    return;
  }
  const phase = classifyAccessPhase(path, sourceItem);
  recordAggregatedAccess(sourceItem, "read", phase, resolution, path.node.loc?.start.line ?? null);
}

function recordAssignmentTargets(path, targetNode, kind, context) {
  const sourceItem = sourceItemForPath(path, context);
  if (!sourceItem || sourceItem.kind === "import") {
    return;
  }
  const phase = classifyAccessPhase(path, sourceItem);
  for (const descriptor of assignmentTargetDescriptors(targetNode)) {
    const resolution = resolveTopLevelBinding(path, descriptor.name, context);
    if (!resolution) {
      continue;
    }
    if (descriptor.kind === "member" || kind === "member_write") {
      recordAggregatedAccess(sourceItem, "member_write", phase, resolution, path.node.loc?.start.line ?? null);
      continue;
    }
    recordAggregatedAccess(sourceItem, "write", phase, resolution, path.node.loc?.start.line ?? null);
  }
}

function recordEffectOccurrence(path, effect, context) {
  const sourceItem = sourceItemForPath(path, context);
  if (!sourceItem || sourceItem.kind === "import") {
    return;
  }
  const phase = classifyAccessPhase(path, sourceItem);
  if (effect === "dynamicImport") {
    const key = phase.phase === "eager" ? "eagerDynamicImportCount" : "lazyDynamicImportCount";
    sourceItem.effects[key]++;
    return;
  }
  if (effect === "containsDirectEval" || effect === "containsImportMeta" || effect === "containsRuntimeSourceRebase") {
    sourceItem.effects[effect] = true;
    return;
  }
  if (phase.phase === "eager") {
    sourceItem.effects[effect] = true;
  }
}

function sourceItemForPath(path, { itemByTopNode, programPathByNode }) {
  const topPath = topLevelProgramChild(path);
  if (!topPath) {
    return null;
  }
  const record = itemByTopNode.get(topPath.node);
  if (record) {
    return record;
  }
  return programPathByNode.get(topPath.node) ? (itemByTopNode.get(topPath.node) ?? null) : null;
}

function isSafeRootRelativeImportMetaUrlUse(metaPath) {
  const importMetaMemberPath = transparentParentPath(metaPath);
  if (!importMetaMemberPath?.isMemberExpression()) {
    return false;
  }
  if (importMetaMemberPath.node.object !== metaPath.node) {
    return false;
  }
  if (importMetaMemberPath.node.computed) {
    if (!t.isStringLiteral(importMetaMemberPath.node.property, { value: "url" })) {
      return false;
    }
  } else if (!t.isIdentifier(importMetaMemberPath.node.property, { name: "url" })) {
    return false;
  }

  const containerPath = transparentParentPath(importMetaMemberPath);
  if (!containerPath?.isNewExpression()) {
    return false;
  }
  if (containerPath.node.arguments?.[1] !== importMetaMemberPath.node) {
    return false;
  }
  if (!t.isIdentifier(containerPath.node.callee, { name: "URL" })) {
    return false;
  }
  const firstArgument = containerPath.node.arguments?.[0];
  return t.isStringLiteral(firstArgument) && firstArgument.value.startsWith("/");
}

function transparentParentPath(path) {
  let current = path.parentPath;
  while (
    current &&
    (current.isParenthesizedExpression?.() ||
      current.isTSAsExpression?.() ||
      current.isTSTypeAssertion?.() ||
      current.isTSNonNullExpression?.())
  ) {
    current = current.parentPath;
  }
  return current;
}

function resolveTopLevelBinding(path, name, { importByLocalName, ownerByBinding }) {
  const binding = path.scope.getBinding(name);
  if (!binding) {
    return unresolvedTopLevelBinding(name, { importByLocalName, ownerByBinding });
  }
  if (!binding.scope.path.isProgram()) {
    return null;
  }
  const owner = ownerByBinding.get(binding.identifier.name);
  if (owner) {
    return {
      kind: "local_declaration",
      name,
      ownerId: owner.id,
      targetType: owner.type,
      targetNames: owner.names,
    };
  }
  const importRecord = importByLocalName.get(binding.identifier.name);
  if (importRecord) {
    return {
      kind: "runtime_import",
      name,
      importId: importRecord.id,
      importKind: importRecord.kind,
      imported: importRecord.imported ?? null,
      importSource: importRecord.source,
    };
  }
  return null;
}

function unresolvedTopLevelBinding(name, { importByLocalName, ownerByBinding }) {
  const owner = ownerByBinding.get(name);
  if (owner) {
    return {
      kind: "local_declaration",
      name,
      ownerId: owner.id,
      targetType: owner.type,
      targetNames: owner.names,
    };
  }
  const importRecord = importByLocalName.get(name);
  if (importRecord) {
    return {
      kind: "runtime_import",
      name,
      importId: importRecord.id,
      importKind: importRecord.kind,
      imported: importRecord.imported ?? null,
      importSource: importRecord.source,
    };
  }
  return null;
}

function classifyAccessPhase(path, sourceItem) {
  if (sourceItem.kind === "side_effect") {
    const nestedFunctionPhase = classifyNestedFunctionPhase(path, sourceItem.path, "side_effect_nested_function_body");
    if (nestedFunctionPhase) {
      return nestedFunctionPhase;
    }
    return {
      phase: "eager",
      siteKind: "top_level_statement",
    };
  }
  if (sourceItem.type === "VariableDeclaration") {
    const nestedFunctionPhase = classifyNestedFunctionPhase(path, sourceItem.path, "variable_nested_function_body");
    if (nestedFunctionPhase) {
      return nestedFunctionPhase;
    }
    return {
      phase: "eager",
      siteKind: "variable_initializer",
    };
  }
  if (sourceItem.type === "FunctionDeclaration") {
    return {
      phase: "lazy",
      siteKind: "function_body",
    };
  }
  if (sourceItem.type !== "ClassDeclaration") {
    return {
      phase: "eager",
      siteKind: "top_level_declaration",
    };
  }

  let current = path;
  let classSite = null;
  while (current.parentPath && current.parentPath !== sourceItem.path.parentPath) {
    const parent = current.parentPath;
    if (parent.isClassMethod() || parent.isClassPrivateMethod() || parent.isObjectMethod()) {
      classSite =
        current.key === "key" && parent.node.computed
          ? { phase: "eager", siteKind: "class_computed_key" }
          : { phase: "lazy", siteKind: "class_method_body" };
      break;
    }
    if (parent.isClassProperty() || parent.isClassPrivateProperty()) {
      if (current.key === "key" && parent.node.computed) {
        classSite = { phase: "eager", siteKind: "class_computed_key" };
        break;
      }
      if (current.key === "value") {
        classSite = parent.node.static
          ? { phase: "eager", siteKind: "class_static_field_initializer" }
          : { phase: "lazy", siteKind: "class_instance_field_initializer" };
        break;
      }
    }
    if (parent.isStaticBlock()) {
      classSite = { phase: "eager", siteKind: "class_static_block" };
      break;
    }
    if ((parent.isClassDeclaration() || parent.isClassExpression()) && current.key === "superClass") {
      classSite = { phase: "eager", siteKind: "class_superclass" };
      break;
    }
    current = parent;
  }

  return classSite ?? { phase: "lazy", siteKind: "class_body" };
}

function classifyNestedFunctionPhase(path, sourcePath, siteKind) {
  const functionParent = path.getFunctionParent();
  if (!functionParent || functionParent === sourcePath) {
    return null;
  }
  if (!functionParent.findParent((parent) => parent === sourcePath)) {
    return null;
  }
  return {
    phase: "lazy",
    siteKind,
  };
}

function recordAggregatedAccess(sourceItem, kind, phase, resolution, line) {
  const targetMap =
    kind === "read"
      ? phase.phase === "eager"
        ? sourceItem.eagerReads
        : sourceItem.lazyReads
      : kind === "write"
        ? phase.phase === "eager"
          ? sourceItem.eagerWrites
          : sourceItem.lazyWrites
        : phase.phase === "eager"
          ? sourceItem.eagerMemberWrites
          : sourceItem.lazyMemberWrites;
  const key = accessAggregationKey(resolution);
  let record = targetMap.get(key);
  if (!record) {
    record = {
      count: 0,
      examples: [],
      kind: resolution.kind,
      line: line ?? null,
      name: resolution.name,
      ownerId: resolution.ownerId ?? null,
      importId: resolution.importId ?? null,
      importKind: resolution.importKind ?? null,
      importSource: resolution.importSource ?? null,
      imported: resolution.imported ?? null,
      siteKinds: new Set(),
      targetNames: resolution.targetNames ?? null,
      targetType: resolution.targetType ?? null,
    };
    targetMap.set(key, record);
  }
  record.count++;
  record.siteKinds.add(phase.siteKind);
  if (line !== null && record.examples.length < 5 && !record.examples.includes(line)) {
    record.examples.push(line);
  }
}

function accessAggregationKey(resolution) {
  return [
    resolution.kind,
    resolution.ownerId ?? "",
    resolution.importId ?? "",
    resolution.importSource ?? "",
    resolution.name,
  ].join(":");
}

function buildLocalReferenceEdges(owners) {
  const edges = new Map();
  for (const owner of owners) {
    recordEdgeSet(edges, owner.id, owner.eagerReads, "eager");
    recordEdgeSet(edges, owner.id, owner.lazyReads, "lazy");
  }
  return finalizeEdgeSet(edges, "reference");
}

function buildMutationEdges(owners) {
  const edges = new Map();
  for (const owner of owners) {
    recordEdgeSet(edges, owner.id, owner.eagerWrites, "eager", "binding");
    recordEdgeSet(edges, owner.id, owner.lazyWrites, "lazy", "binding");
    recordEdgeSet(edges, owner.id, owner.eagerMemberWrites, "eager", "member");
    recordEdgeSet(edges, owner.id, owner.lazyMemberWrites, "lazy", "member");
  }
  return finalizeEdgeSet(edges, "mutation");
}

function recordEdgeSet(edges, sourceOwnerId, accessMap, phase, mutationKind = null) {
  for (const access of accessMap.values()) {
    if (access.kind !== "local_declaration" || !access.ownerId) {
      continue;
    }
    const key = `${sourceOwnerId}:${access.ownerId}`;
    let edge = edges.get(key);
    if (!edge) {
      edge = {
        from: sourceOwnerId,
        to: access.ownerId,
        count: 0,
        phases: new Set(),
        names: new Set(),
        siteKinds: new Set(),
        writeKinds: new Set(),
      };
      edges.set(key, edge);
    }
    edge.count += access.count;
    edge.phases.add(phase);
    edge.names.add(access.name);
    for (const siteKind of access.siteKinds) {
      edge.siteKinds.add(siteKind);
    }
    if (mutationKind) {
      edge.writeKinds.add(mutationKind);
    }
  }
}

function finalizeEdgeSet(edges, kind) {
  return [...edges.values()]
    .map((edge) => ({
      kind,
      from: edge.from,
      to: edge.to,
      count: edge.count,
      names: [...edge.names].sort(),
      phases: [...edge.phases].sort(),
      siteKinds: [...edge.siteKinds].sort(),
      ...(edge.writeKinds.size > 0 ? { writeKinds: [...edge.writeKinds].sort() } : {}),
    }))
    .sort(compareEdges);
}

function buildInteractionEdges(referenceEdges, mutationEdges) {
  const combined = new Map();
  for (const edge of [...referenceEdges, ...mutationEdges]) {
    const key = `${edge.from}:${edge.to}`;
    let record = combined.get(key);
    if (!record) {
      record = {
        from: edge.from,
        to: edge.to,
        count: 0,
        kinds: new Set(),
        names: new Set(),
        phases: new Set(),
        siteKinds: new Set(),
        writeKinds: new Set(),
      };
      combined.set(key, record);
    }
    record.count += edge.count;
    record.kinds.add(edge.kind);
    for (const name of edge.names) {
      record.names.add(name);
    }
    for (const phase of edge.phases) {
      record.phases.add(phase);
    }
    for (const siteKind of edge.siteKinds) {
      record.siteKinds.add(siteKind);
    }
    for (const writeKind of edge.writeKinds ?? []) {
      record.writeKinds.add(writeKind);
    }
  }
  return [...combined.values()]
    .map((edge) => ({
      kind: "interaction",
      from: edge.from,
      to: edge.to,
      count: edge.count,
      interactionKinds: [...edge.kinds].sort(),
      names: [...edge.names].sort(),
      phases: [...edge.phases].sort(),
      siteKinds: [...edge.siteKinds].sort(),
      ...(edge.writeKinds.size > 0 ? { writeKinds: [...edge.writeKinds].sort() } : {}),
    }))
    .sort(compareEdges);
}

function buildEagerInitEdges(referenceEdges, mutationEdges) {
  const eager = new Map();
  for (const edge of [...referenceEdges, ...mutationEdges]) {
    if (!edge.phases.includes("eager")) {
      continue;
    }
    const key = `${edge.from}:${edge.to}`;
    let record = eager.get(key);
    if (!record) {
      record = {
        from: edge.from,
        to: edge.to,
        count: 0,
        sources: new Set(),
        names: new Set(),
        siteKinds: new Set(),
        writeKinds: new Set(),
      };
      eager.set(key, record);
    }
    record.count += edge.count;
    record.sources.add(edge.kind);
    for (const name of edge.names) {
      record.names.add(name);
    }
    for (const siteKind of edge.siteKinds) {
      record.siteKinds.add(siteKind);
    }
    for (const writeKind of edge.writeKinds ?? []) {
      record.writeKinds.add(writeKind);
    }
  }
  return [...eager.values()]
    .map((edge) => ({
      kind: "eager_init",
      from: edge.from,
      to: edge.to,
      count: edge.count,
      names: [...edge.names].sort(),
      siteKinds: [...edge.siteKinds].sort(),
      sources: [...edge.sources].sort(),
      ...(edge.writeKinds.size > 0 ? { writeKinds: [...edge.writeKinds].sort() } : {}),
    }))
    .sort(compareEdges);
}

function compareEdges(left, right) {
  return left.from.localeCompare(right.from) || left.to.localeCompare(right.to);
}

function finalizeOwnerExtractorCompatibility(owners, sideEffects) {
  const writeSourcesByOwner = buildBindingWriteSourcesByOwner([...owners, ...sideEffects]);
  for (const owner of owners) {
    owner.currentExtractorBlockingReasons = extractorCompatibilityReasonsForOwner(owner, writeSourcesByOwner);
    owner.currentExtractorCompatible = owner.currentExtractorBlockingReasons.length === 0;
    owner.currentExtractorLowering = owner.currentExtractorCompatible
      ? extractorLoweringForOwner(owner, writeSourcesByOwner)
      : "blocked";
  }
}

function extractorCompatibilityReasonsForOwner(owner, writeSourcesByOwner) {
  if (owner.type !== "VariableDeclaration") {
    return [];
  }
  const declaration = unwrapTopLevelDeclarationNode(owner.node);
  if (!t.isVariableDeclaration(declaration)) {
    return [`unexpected_statement_type:${declaration?.type ?? "unknown"}`];
  }
  const declaredNames = declaration.declarations.flatMap((entry) => bindingNames(entry.id));
  const declaredNameSet = new Set(declaredNames);
  const availableNames = new Set();
  for (const entry of declaration.declarations) {
    for (const referencedName of referencedUndeclaredNamesInVariableDeclarator(entry)) {
      if (!declaredNameSet.has(referencedName)) {
        continue;
      }
      if (!availableNames.has(referencedName)) {
        return snapshotCompatibleForwardSelfReferenceReasons(owner, writeSourcesByOwner, referencedName);
      }
    }
    for (const declaredName of bindingNames(entry.id)) {
      availableNames.add(declaredName);
    }
  }
  return [];
}

function extractorLoweringForOwner(owner, writeSourcesByOwner) {
  if (owner.type !== "VariableDeclaration") {
    return "standard";
  }
  const declaration = unwrapTopLevelDeclarationNode(owner.node);
  if (!t.isVariableDeclaration(declaration)) {
    return "blocked";
  }
  const declaredNames = declaration.declarations.flatMap((entry) => bindingNames(entry.id));
  const declaredNameSet = new Set(declaredNames);
  const availableNames = new Set();
  for (const entry of declaration.declarations) {
    for (const referencedName of referencedUndeclaredNamesInVariableDeclarator(entry)) {
      if (!declaredNameSet.has(referencedName)) {
        continue;
      }
      if (!availableNames.has(referencedName)) {
        return isSnapshotCompatibleVariableOwner(owner, writeSourcesByOwner)
          ? "snapshot_variable_declaration"
          : "blocked";
      }
    }
    for (const declaredName of bindingNames(entry.id)) {
      availableNames.add(declaredName);
    }
  }
  return "standard";
}

function unwrapTopLevelDeclarationNode(node) {
  if (t.isExportNamedDeclaration(node) && node.declaration) {
    return node.declaration;
  }
  return node;
}

function buildBindingWriteSourcesByOwner(records) {
  const writesByOwner = new Map();
  for (const record of records) {
    for (const access of [...record.eagerWrites.values(), ...record.lazyWrites.values()]) {
      if (access.kind !== "local_declaration" || !access.ownerId) {
        continue;
      }
      if (!writesByOwner.has(access.ownerId)) {
        writesByOwner.set(access.ownerId, new Set());
      }
      writesByOwner.get(access.ownerId).add(record.id);
    }
  }
  return writesByOwner;
}

function isSnapshotCompatibleVariableOwner(owner, writeSourcesByOwner) {
  return (writeSourcesByOwner.get(owner.id)?.size ?? 0) === 0;
}

function snapshotCompatibleForwardSelfReferenceReasons(owner, writeSourcesByOwner, referencedName) {
  if (isSnapshotCompatibleVariableOwner(owner, writeSourcesByOwner)) {
    return [];
  }
  return [`forward_or_self_variable_reference:${referencedName}`];
}

function finalizeOwnerModes(owners) {
  const ownerById = new Map(owners.map((owner) => [owner.id, owner]));
  const interactionAdjacency = new Map(owners.map((owner) => [owner.id, collectLocalInteractionTargets(owner)]));

  let plainSeeds = new Set(owners.filter((owner) => isBasePlainImportEligible(owner)).map((owner) => owner.id));
  let changed = true;
  while (changed) {
    changed = false;
    for (const ownerId of [...plainSeeds]) {
      const dependencies = interactionAdjacency.get(ownerId) ?? new Set();
      if ([...dependencies].some((dependencyId) => !plainSeeds.has(dependencyId))) {
        plainSeeds.delete(ownerId);
        changed = true;
      }
    }
  }

  for (const owner of owners) {
    const reasons = extractionReasonsForOwner(owner);
    if (RUNTIME_SENSITIVE_EFFECTSHasAny(owner.effects)) {
      owner.extractionMode = "keep_runtime";
      owner.extractionReasons = reasons;
      continue;
    }
    if (plainSeeds.has(owner.id)) {
      owner.extractionMode = "plain_import_candidate";
      owner.extractionReasons = reasons.filter((reason) => !reason.startsWith("blocked_by_non_plain_dependency"));
      continue;
    }
    owner.extractionMode = "selected_module_candidate";
    const blockingDependencies = [...(interactionAdjacency.get(owner.id) ?? new Set())]
      .filter((dependencyId) => !plainSeeds.has(dependencyId))
      .sort();
    owner.extractionReasons = [
      ...reasons,
      ...(blockingDependencies.length > 0 ? [`blocked_by_non_plain_dependency:${blockingDependencies.join(",")}`] : []),
    ];
  }
}

function RUNTIME_SENSITIVE_EFFECTSHasAny(effects) {
  return [...RUNTIME_SENSITIVE_EFFECTS].some((effect) => effects[effect]);
}

function isBasePlainImportEligible(owner) {
  if (RUNTIME_SENSITIVE_EFFECTSHasAny(owner.effects)) {
    return false;
  }
  if (owner.type === "FunctionDeclaration") {
    return true;
  }
  if (owner.type === "ClassDeclaration") {
    return (
      !owner.classFeatures.hasSuperClass &&
      owner.classFeatures.staticBlockCount === 0 &&
      owner.classFeatures.staticFieldCount === 0 &&
      owner.classFeatures.computedKeyCount === 0 &&
      owner.eagerWrites.size === 0 &&
      owner.eagerMemberWrites.size === 0 &&
      owner.eagerReads.size === 0
    );
  }
  return false;
}

function extractionReasonsForOwner(owner) {
  const reasons = [];
  if (owner.type === "VariableDeclaration") {
    reasons.push("unsupported_plain_import:VariableDeclaration");
  }
  if (owner.type === "ClassDeclaration") {
    if (owner.classFeatures.hasSuperClass) {
      reasons.push("class_has_superclass");
    }
    if (owner.classFeatures.staticFieldCount > 0) {
      reasons.push("class_has_static_field_initializer");
    }
    if (owner.classFeatures.staticBlockCount > 0) {
      reasons.push("class_has_static_block");
    }
    if (owner.classFeatures.computedKeyCount > 0) {
      reasons.push("class_has_computed_key");
    }
  }
  for (const effect of [...RUNTIME_SENSITIVE_EFFECTS].filter((name) => owner.effects[name])) {
    reasons.push(effectToReason(effect));
  }
  if (owner.eagerReads.size > 0) {
    reasons.push(`eager_reads_top_level:${sortedAccessNames(owner.eagerReads).join(",")}`);
  }
  if (owner.eagerWrites.size > 0) {
    reasons.push(`eager_writes_top_level:${sortedAccessNames(owner.eagerWrites).join(",")}`);
  }
  if (owner.eagerMemberWrites.size > 0) {
    reasons.push(`eager_member_writes_top_level:${sortedAccessNames(owner.eagerMemberWrites).join(",")}`);
  }
  return [...new Set(reasons)];
}

function effectToReason(effect) {
  if (effect === "containsDirectEval") {
    return "contains_direct_eval";
  }
  if (effect === "containsImportMeta") {
    return "contains_import_meta";
  }
  if (effect === "containsTopLevelAwait") {
    return "contains_top_level_await";
  }
  return effect;
}

function sortedAccessNames(accessMap) {
  return [...new Set([...accessMap.values()].map((access) => access.name))].sort();
}

function collectLocalInteractionTargets(owner) {
  return new Set([
    ...collectLocalTargets(owner.eagerReads),
    ...collectLocalTargets(owner.lazyReads),
    ...collectLocalTargets(owner.eagerWrites),
    ...collectLocalTargets(owner.lazyWrites),
    ...collectLocalTargets(owner.eagerMemberWrites),
    ...collectLocalTargets(owner.lazyMemberWrites),
  ]);
}

function collectLocalTargets(accessMap) {
  return [...accessMap.values()]
    .filter((access) => access.kind === "local_declaration" && access.ownerId)
    .map((access) => access.ownerId);
}

function buildOwnerComponents(owners, edges, prefix) {
  const adjacency = new Map(owners.map((owner) => [owner.id, new Set()]));
  for (const edge of edges) {
    adjacency.get(edge.from)?.add(edge.to);
  }
  const components = stronglyConnectedComponents(
    owners.map((owner) => owner.id),
    adjacency
  );
  const ownerById = new Map(owners.map((owner) => [owner.id, owner]));
  return components
    .sort((left, right) => minOwnerOrdinal(left, ownerById) - minOwnerOrdinal(right, ownerById))
    .map((component, index) => finalizeComponent(component, index, prefix, ownerById, edges));
}

function buildWeakOwnerComponents(owners, edges, prefix) {
  const adjacency = new Map(owners.map((owner) => [owner.id, new Set()]));
  for (const edge of edges) {
    adjacency.get(edge.from)?.add(edge.to);
    adjacency.get(edge.to)?.add(edge.from);
  }
  const components = connectedComponents(
    owners.map((owner) => owner.id),
    adjacency
  );
  const ownerById = new Map(owners.map((owner) => [owner.id, owner]));
  return components
    .sort((left, right) => minOwnerOrdinal(left, ownerById) - minOwnerOrdinal(right, ownerById))
    .map((component, index) => finalizeComponent(component, index, prefix, ownerById, edges, { cyclic: false }));
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

function connectedComponents(nodes, edges) {
  const seen = new Set();
  const components = [];
  for (const node of nodes) {
    if (seen.has(node)) {
      continue;
    }
    const stack = [node];
    const component = [];
    seen.add(node);
    while (stack.length > 0) {
      const current = stack.pop();
      component.push(current);
      for (const next of edges.get(current) ?? []) {
        if (seen.has(next)) {
          continue;
        }
        seen.add(next);
        stack.push(next);
      }
    }
    components.push(component);
  }
  return components;
}

function minOwnerOrdinal(component, ownerById) {
  return Math.min(...component.map((ownerId) => ownerById.get(ownerId)?.ordinal ?? Number.MAX_SAFE_INTEGER));
}

function finalizeComponent(component, index, prefix, ownerById, edges, { cyclic = component.length > 1 } = {}) {
  const ownerIds = [...component].sort(
    (left, right) => (ownerById.get(left)?.ordinal ?? 0) - (ownerById.get(right)?.ordinal ?? 0)
  );
  const members = ownerIds.map((ownerId) => ownerById.get(ownerId));
  const dependencyIds = new Set();
  for (const edge of edges) {
    if (!component.includes(edge.from) || component.includes(edge.to)) {
      continue;
    }
    dependencyIds.add(edge.to);
  }
  return {
    id: `${prefix}_${index.toString().padStart(4, "0")}`,
    ownerIds,
    lines: members.map((owner) => owner.line).filter((line) => line !== null),
    cyclic,
    memberNames: members.flatMap((owner) => owner.names).sort(),
    modes: [...new Set(members.map((owner) => owner.extractionMode))].sort(),
    reasons: [...new Set(members.flatMap((owner) => owner.extractionReasons))].sort(),
    dependencyOwnerIds: [...dependencyIds].sort(),
  };
}

function indexComponentsByOwnerId(components) {
  const index = new Map();
  for (const component of components) {
    for (const ownerId of component.ownerIds) {
      index.set(ownerId, component.id);
    }
  }
  return index;
}

function buildSelectedModuleRegions({ weakInteractionComponents, owners, sideEffects, programItems }) {
  const ownerById = new Map(owners.map((owner) => [owner.id, owner]));
  const programItemByOrdinal = new Map(programItems.map((item) => [item.ordinal, item]));
  const weakInteractionComponentIndex = indexComponentsByOwnerId(weakInteractionComponents);
  const ownersByWeakInteractionComponentId = new Map();

  for (const owner of owners) {
    const componentId = weakInteractionComponentIndex.get(owner.id);
    if (!componentId) {
      continue;
    }
    if (!ownersByWeakInteractionComponentId.has(componentId)) {
      ownersByWeakInteractionComponentId.set(componentId, []);
    }
    ownersByWeakInteractionComponentId.get(componentId).push(owner);
  }

  const regions = [];
  for (const [weakInteractionComponentId, regionOwners] of ownersByWeakInteractionComponentId.entries()) {
    const sortedOwners = [...regionOwners].sort((left, right) => left.ordinal - right.ordinal);
    let currentRegionOwners = [];
    for (const owner of sortedOwners) {
      if (
        currentRegionOwners.length > 0 &&
        !canAppendOrderedInitRegionOwner(currentRegionOwners.at(-1), owner, weakInteractionComponentId, {
          ownerById,
          programItemByOrdinal,
          weakInteractionComponentIndex,
        })
      ) {
        regions.push(
          finalizeOrderedInitRegion({
            index: regions.length,
            owners,
            ownerById,
            regionOwners: currentRegionOwners,
            sideEffects,
          })
        );
        currentRegionOwners = [];
      }
      currentRegionOwners.push(owner);
    }
    if (currentRegionOwners.length > 0) {
      regions.push(
        finalizeOrderedInitRegion({
          index: regions.length,
          owners,
          ownerById,
          regionOwners: currentRegionOwners,
          sideEffects,
        })
      );
    }
  }

  return regions.sort((left, right) => left.startOrdinal - right.startOrdinal);
}

function canAppendOrderedInitRegionOwner(
  previousOwner,
  nextOwner,
  weakInteractionComponentId,
  { ownerById, programItemByOrdinal, weakInteractionComponentIndex }
) {
  for (let ordinal = previousOwner.ordinal + 1; ordinal < nextOwner.ordinal; ordinal++) {
    const item = programItemByOrdinal.get(ordinal);
    if (!item || item.kind !== "declaration") {
      return false;
    }
    const owner = ownerById.get(item.id);
    if (!owner || weakInteractionComponentIndex.get(owner.id) !== weakInteractionComponentId) {
      return false;
    }
  }
  return true;
}

function finalizeOrderedInitRegion({ index, owners, ownerById, regionOwners, sideEffects }) {
  const selectedOwnerIds = new Set(regionOwners.map((owner) => owner.id));
  const startOrdinal = regionOwners[0].ordinal;
  const endOrdinal = regionOwners.at(-1).ordinal;
  const runtimeImportLocals = new Set();
  const outsideLocalDependencyOwnerIds = new Set();
  const outsideLocalDependencyNames = new Set();
  const earlierEagerUseItemIds = new Set();
  const outsideWriteItemIds = new Set();
  const runtimeSensitiveOwnerIds = new Set();
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
    for (const effect of [...RUNTIME_SENSITIVE_EFFECTS].filter((effect) => owner.effects[effect])) {
      runtimeSensitiveOwnerIds.add(owner.id);
    }

    for (const access of selectedModuleAccesses(owner)) {
      if (access.kind === "runtime_import") {
        runtimeImportLocals.add(access.name);
        if (access.label === "eager write" || access.label === "lazy write") {
          unsupportedRuntimeImportWrites.add(access.name);
        }
        continue;
      }
      if (access.kind !== "local_declaration" || !access.ownerId || selectedOwnerIds.has(access.ownerId)) {
        continue;
      }
      outsideLocalDependencyOwnerIds.add(access.ownerId);
      outsideLocalDependencyNames.add(access.name);
    }

    for (const access of selectedModuleForwardDependencyAccesses(owner)) {
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

  for (const record of [...owners, ...sideEffects]) {
    if (selectedOwnerIds.has(record.id)) {
      continue;
    }
    for (const access of [...record.eagerWrites.values(), ...record.lazyWrites.values()]) {
      if (access.kind === "local_declaration" && access.ownerId && selectedOwnerIds.has(access.ownerId)) {
        outsideWriteItemIds.add(record.id);
      }
    }
    if (record.ordinal >= startOrdinal) {
      continue;
    }
    for (const access of [...record.eagerReads.values(), ...record.eagerMemberWrites.values()]) {
      if (access.kind === "local_declaration" && access.ownerId && selectedOwnerIds.has(access.ownerId)) {
        earlierEagerUseItemIds.add(record.id);
      }
    }
  }

  const blockingReasons = [
    ...(unsupportedOwnerIds.size > 0 ? [`unsupported_owner:${[...unsupportedOwnerIds].sort().join(",")}`] : []),
    ...(runtimeSensitiveOwnerIds.size > 0
      ? [`runtime_sensitive_owner:${[...runtimeSensitiveOwnerIds].sort().join(",")}`]
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

  return {
    id: `selected_module_region_${index.toString().padStart(4, "0")}`,
    ownerIds: regionOwners.map((owner) => owner.id),
    startOrdinal,
    endOrdinal,
    lines: regionOwners.map((owner) => owner.line).filter((line) => line !== null),
    memberNames: regionOwners.flatMap((owner) => owner.names).sort(),
    modes: [...new Set(regionOwners.map((owner) => owner.extractionMode))].sort(),
    runtimeImportLocals: [...runtimeImportLocals].sort(),
    outsideLocalDependencyOwnerIds: [...outsideLocalDependencyOwnerIds].sort(),
    outsideLocalDependencyNames: [...outsideLocalDependencyNames].sort(),
    outsideWriteItemIds: [...outsideWriteItemIds].sort(),
    earlierEagerUseItemIds: [...earlierEagerUseItemIds].sort(),
    unsupportedOwnerIds: [...unsupportedOwnerIds].sort(),
    runtimeSensitiveOwnerIds: [...runtimeSensitiveOwnerIds].sort(),
    unsupportedForwardEdges: [...unsupportedForwardEdges].sort(),
    selectedModuleExtractable: blockingReasons.length === 0,
    selectedModuleBlockingReasons: blockingReasons.sort(),
  };
}

function selectedModuleAccesses(owner) {
  return [
    ...owner.eagerReads.values(),
    ...owner.lazyReads.values(),
    ...owner.eagerWrites.values(),
    ...owner.lazyWrites.values(),
    ...owner.eagerMemberWrites.values(),
    ...owner.lazyMemberWrites.values(),
  ];
}

function selectedModuleForwardDependencyAccesses(owner) {
  return [...owner.eagerReads.values(), ...owner.eagerWrites.values(), ...owner.eagerMemberWrites.values()];
}

function buildComponentDag(components, edges) {
  const componentByOwnerId = indexComponentsByOwnerId(components);
  const dag = new Map();
  for (const component of components) {
    dag.set(component.id, new Set());
  }
  for (const edge of edges) {
    const fromComponent = componentByOwnerId.get(edge.from);
    const toComponent = componentByOwnerId.get(edge.to);
    if (!fromComponent || !toComponent || fromComponent === toComponent) {
      continue;
    }
    dag.get(fromComponent).add(toComponent);
  }
  return [...dag.entries()]
    .map(([componentId, dependencyIds]) => ({
      componentId,
      dependencyComponentIds: [...dependencyIds].sort(),
    }))
    .sort((left, right) => left.componentId.localeCompare(right.componentId));
}

function finalizeOwnerRecord(owner) {
  return {
    id: owner.id,
    line: owner.line,
    ordinal: owner.ordinal,
    type: owner.type,
    names: owner.names,
    classFeatures: owner.classFeatures,
    effects: owner.effects,
    readsTopLevel: finalizeAccessBuckets(owner.eagerReads, owner.lazyReads),
    writesTopLevel: finalizeAccessBuckets(owner.eagerWrites, owner.lazyWrites),
    memberWritesTopLevel: finalizeAccessBuckets(owner.eagerMemberWrites, owner.lazyMemberWrites),
    extractionMode: owner.extractionMode,
    extractionReasons: owner.extractionReasons,
    currentExtractorBlockingReasons: owner.currentExtractorBlockingReasons,
    currentExtractorCompatible: owner.currentExtractorCompatible,
    currentExtractorLowering: owner.currentExtractorLowering,
    eagerInitComponentId: owner.eagerInitComponentId,
    interactionComponentId: owner.interactionComponentId,
  };
}

function finalizeSideEffectRecord(sideEffect) {
  return {
    id: sideEffect.id,
    line: sideEffect.line,
    ordinal: sideEffect.ordinal,
    type: sideEffect.type,
    effects: sideEffect.effects,
    readsTopLevel: finalizeAccessBuckets(sideEffect.eagerReads, sideEffect.lazyReads),
    writesTopLevel: finalizeAccessBuckets(sideEffect.eagerWrites, sideEffect.lazyWrites),
    memberWritesTopLevel: finalizeAccessBuckets(sideEffect.eagerMemberWrites, sideEffect.lazyMemberWrites),
    extractionMode: sideEffect.extractionMode,
    extractionReasons: sideEffect.extractionReasons,
  };
}

function finalizeProgramItem(item, owners, sideEffects) {
  if (item.kind === "import") {
    return item;
  }
  if (item.kind === "declaration") {
    const owner = owners.find((record) => record.id === item.id);
    return {
      ...item,
      eagerInitComponentId: owner?.eagerInitComponentId ?? null,
      extractionMode: owner?.extractionMode ?? null,
    };
  }
  const sideEffect = sideEffects.find((record) => record.id === item.id);
  return {
    ...item,
    extractionMode: sideEffect?.extractionMode ?? "keep_runtime",
  };
}

function finalizeAccessBuckets(eagerMap, lazyMap) {
  return {
    eager: finalizeAccessMap(eagerMap),
    lazy: finalizeAccessMap(lazyMap),
  };
}

function finalizeAccessMap(accessMap) {
  return [...accessMap.values()]
    .map((access) => ({
      kind: access.kind,
      name: access.name,
      ownerId: access.ownerId,
      importId: access.importId,
      importKind: access.importKind,
      importSource: access.importSource,
      imported: access.imported,
      targetType: access.targetType,
      targetNames: access.targetNames,
      count: access.count,
      siteKinds: [...access.siteKinds].sort(),
      examples: [...access.examples].sort((left, right) => left - right),
    }))
    .sort(compareAccessRecords);
}

function compareAccessRecords(left, right) {
  return (
    (left.ownerId ?? "").localeCompare(right.ownerId ?? "") ||
    (left.importSource ?? "").localeCompare(right.importSource ?? "") ||
    left.name.localeCompare(right.name)
  );
}

function summarizeBoundaryCounts({
  eagerInitDag,
  eagerInitEdges,
  eagerInitSccs,
  imports,
  interactionEdges,
  interactionSccs,
  selectedModuleRegions,
  ownerOutput,
  programItemOutput,
  sideEffectOutput,
}) {
  return {
    declarationOwners: ownerOutput.length,
    eagerInitComponents: eagerInitSccs.length,
    eagerInitDagEdges: eagerInitDag.reduce((count, entry) => count + entry.dependencyComponentIds.length, 0),
    eagerInitEdges: eagerInitEdges.length,
    imports: imports.length,
    interactionComponents: interactionSccs.length,
    interactionEdges: interactionEdges.length,
    keepRuntimeCandidates: ownerOutput.filter((owner) => owner.extractionMode === "keep_runtime").length,
    selectedModuleExtractableRegions: selectedModuleRegions.filter((region) => region.selectedModuleExtractable).length,
    selectedModuleRegions: selectedModuleRegions.length,
    selectedModuleCandidates: ownerOutput.filter((owner) => owner.extractionMode === "selected_module_candidate")
      .length,
    plainImportCandidates: ownerOutput.filter((owner) => owner.extractionMode === "plain_import_candidate").length,
    programItems: programItemOutput.length,
    sideEffects: sideEffectOutput.length,
  };
}

function buildBoundarySummary({ chunkSummaries, inputRoot, inputManifestPath, outDir }) {
  return {
    schemaVersion: 1,
    kind: "js.runtime_boundary_summary",
    inputRoot: inputRoot ? relativeWorkspacePath(inputRoot) : null,
    outDir: relativeWorkspacePath(outDir),
    inputManifestPath: inputManifestPath ? relativeWorkspacePath(inputManifestPath) : null,
    counts: {
      chunks: chunkSummaries.length,
      declarationOwners: chunkSummaries.reduce((count, chunk) => count + chunk.counts.declarationOwners, 0),
      eagerInitComponents: chunkSummaries.reduce((count, chunk) => count + chunk.counts.eagerInitComponents, 0),
      keepRuntimeCandidates: chunkSummaries.reduce((count, chunk) => count + chunk.counts.keepRuntimeCandidates, 0),
      selectedModuleExtractableRegions: chunkSummaries.reduce(
        (count, chunk) => count + chunk.counts.selectedModuleExtractableRegions,
        0
      ),
      selectedModuleRegions: chunkSummaries.reduce((count, chunk) => count + chunk.counts.selectedModuleRegions, 0),
      selectedModuleCandidates: chunkSummaries.reduce(
        (count, chunk) => count + chunk.counts.selectedModuleCandidates,
        0
      ),
      plainImportCandidates: chunkSummaries.reduce((count, chunk) => count + chunk.counts.plainImportCandidates, 0),
      sideEffects: chunkSummaries.reduce((count, chunk) => count + chunk.counts.sideEffects, 0),
    },
    chunks: chunkSummaries
      .map((chunk) => ({
        chunkId: chunk.chunkId,
        counts: chunk.counts,
        runtimePath: chunk.runtimePath,
      }))
      .sort((left, right) => left.chunkId.localeCompare(right.chunkId)),
  };
}

function describeArtifactChunkPath(inputRoot, chunkId, file) {
  if (!inputRoot) {
    return `${chunkId}/${file}`;
  }
  return relativeWorkspacePath(join(inputRoot, ...chunkId.split("/"), file));
}

function describeClassFeatures(node) {
  const features = emptyClassFeatures();
  features.hasSuperClass = node.superClass !== null;
  for (const element of node.body.body) {
    if (t.isStaticBlock?.(element) || element.type === "StaticBlock") {
      features.staticBlockCount++;
      continue;
    }
    if ("computed" in element && element.computed) {
      features.computedKeyCount++;
    }
    if (element.type === "ClassProperty" || element.type === "ClassPrivateProperty") {
      if (element.static) {
        features.staticFieldCount++;
      } else {
        features.instanceFieldCount++;
      }
    }
  }
  return features;
}
