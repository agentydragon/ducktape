import { existsSync, mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, posix } from "node:path";
import generateModule from "@babel/generator";
import { parse } from "@babel/parser";
import traverseModule from "@babel/traverse";
import * as t from "@babel/types";
import { DEFAULT_PARSER_OPTIONS } from "../common/parser_options.mjs";
import {
  analyzeProgramShallow,
  bindingNames,
  buildChunkManifestFromAnalysis,
  describeImport,
  specifierName,
  topLevelDeclarationNames,
} from "../common/program_analysis.mjs";
import { CANONICAL_CHUNK_ENTRY_FILE, normalizeChunkEntryFile } from "../common/normalize.mjs";
import { requireValue, resolveWorkspacePath } from "../common/io.mjs";

const generate = generateModule.default ?? generateModule;
const traverse = traverseModule.default ?? traverseModule;

export function parseArgs(argv) {
  const options = {
    chunkId: undefined,
    entryFile: CANONICAL_CHUNK_ENTRY_FILE,
    force: false,
    help: false,
    inputPath: undefined,
    outDir: undefined,
  };

  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    switch (arg) {
      case "--chunk-id":
        options.chunkId = requireValue(argv, ++index, arg);
        break;
      case "--force":
        options.force = true;
        break;
      case "--entry-file":
        options.entryFile = requireValue(argv, ++index, arg);
        break;
      case "--help":
      case "-h":
        options.help = true;
        break;
      case "--input":
        options.inputPath = resolveWorkspacePath(requireValue(argv, ++index, arg));
        break;
      case "--out":
        options.outDir = resolveWorkspacePath(requireValue(argv, ++index, arg));
        break;
      default:
        throw new Error(`Unknown argument: ${arg}`);
    }
  }

  if (options.help) {
    return options;
  }
  if (!options.inputPath) {
    throw new Error("--input is required");
  }
  if (!options.outDir) {
    throw new Error("--out is required");
  }
  options.entryFile = normalizeChunkEntryFile(options.entryFile);
  return options;
}

export function splitScopeHoistedChunk(
  code,
  {
    chunkId,
    entryFile = CANONICAL_CHUNK_ENTRY_FILE,
    includeJsFileAsts = false,
    rewriteEntryImportSource = undefined,
    rewriteRuntimeImportSource = undefined,
    sourcePath,
  }
) {
  const normalizedEntryFile = normalizeChunkEntryFile(entryFile);
  const ast = parse(code, DEFAULT_PARSER_OPTIONS);
  return splitScopeHoistedChunkAst(ast, {
    chunkId,
    entryFile: normalizedEntryFile,
    includeJsFileAsts,
    rewriteEntryImportSource: rewriteEntryImportSource ?? rewriteRuntimeImportSource ?? identity,
    sourcePath,
  });
}

export function splitScopeHoistedChunkAst(
  ast,
  {
    chunkId,
    entryFile = CANONICAL_CHUNK_ENTRY_FILE,
    includeJsFileAsts = false,
    rewriteEntryImportSource = identity,
    sourcePath,
  }
) {
  const normalizedEntryFile = normalizeChunkEntryFile(entryFile);
  const clonedAst = t.cloneNode(ast, true);

  const analysis = analyzeProgram(clonedAst);
  const plan = buildExecutableSplitPlan(analysis);
  const generatedFiles = new Map();
  const generatedJsFiles = new Map();

  generatedJsFiles.set(normalizedEntryFile, buildEntryFile(clonedAst, plan, rewriteEntryImportSource));
  for (const part of plan.parts) {
    generatedJsFiles.set(part.file, buildPartFile(part, plan, rewriteEntryImportSource));
  }
  for (const [file, jsFile] of generatedJsFiles.entries()) {
    generatedFiles.set(file, serializeGeneratedJsFile(jsFile));
  }

  const manifest = buildChunkManifestFromAnalysis(chunkId, normalizedEntryFile, sourcePath, analysis, plan);
  generatedFiles.set("manifest.json", `${JSON.stringify(manifest, null, 2)}\n`);

  return {
    files: generatedFiles,
    ...(includeJsFileAsts ? { jsFiles: generatedJsFiles } : {}),
    manifest,
  };
}

function identity(value) {
  return value;
}

// Full program analysis for split: shallow pass (in common/program_analysis.mjs)
// + a babel-traverse pass that walks every body to record owner-to-owner
// dependencies. Only split needs the dep graph; normalize uses analyzeProgramShallow
// directly.
function analyzeProgram(ast) {
  const analysis = analyzeProgramShallow(ast);
  const { importByLocalName, ownerByBinding, ownerByTopNode, ownerEdges, sideEffectByTopNode } = analysis;
  let dynamicImportCount = 0;

  const recordTopLevelBindingUse = (path, bindingName) => {
    const binding = path.scope.getBinding(bindingName);
    if (!binding || !binding.scope.path.isProgram()) {
      return;
    }
    const topPath = topLevelProgramChild(path);
    if (!topPath) {
      return;
    }
    const targetOwner = ownerByBinding.get(binding.identifier.name);
    const importRecord = importByLocalName.get(binding.identifier.name);
    const sourceOwner = ownerByTopNode.get(topPath.node);
    const sourceSideEffect = sideEffectByTopNode.get(topPath.node);
    if (sourceOwner && targetOwner && sourceOwner.id !== targetOwner.id) {
      addBindingDependency(sourceOwner, targetOwner, binding.identifier.name);
      ownerEdges.get(sourceOwner.id).add(targetOwner.id);
    } else if (sourceOwner && importRecord) {
      sourceOwner.externalImports.set(importRecord.local, importRecord);
    } else if (sourceSideEffect && targetOwner) {
      sourceSideEffect.dependencies.add(binding.identifier.name);
    } else if (sourceSideEffect && importRecord) {
      sourceSideEffect.externalImports.set(importRecord.local, importRecord);
    }
  };

  traverse(ast, {
    AssignmentExpression(path) {
      for (const name of assignmentTargetNames(path.node.left)) {
        recordTopLevelBindingUse(path, name);
      }
    },
    ForInStatement(path) {
      for (const name of loopAssignmentTargetNames(path.node.left)) {
        recordTopLevelBindingUse(path, name);
      }
    },
    ForOfStatement(path) {
      for (const name of loopAssignmentTargetNames(path.node.left)) {
        recordTopLevelBindingUse(path, name);
      }
    },
    Import() {
      dynamicImportCount++;
    },
    ReferencedIdentifier(path) {
      recordTopLevelBindingUse(path, path.node.name);
    },
    UpdateExpression(path) {
      for (const name of assignmentTargetNames(path.node.argument)) {
        recordTopLevelBindingUse(path, name);
      }
    },
  });

  analysis.dynamicImportCount = dynamicImportCount;
  return analysis;
}

function assignmentTargetNames(node) {
  if (!node) {
    return [];
  }
  if (
    node.type === "Identifier" ||
    node.type === "ArrayPattern" ||
    node.type === "ObjectPattern" ||
    node.type === "RestElement" ||
    node.type === "AssignmentPattern"
  ) {
    return bindingNames(node);
  }
  if (
    node.type === "TSAsExpression" ||
    node.type === "TSTypeAssertion" ||
    node.type === "TSNonNullExpression" ||
    node.type === "ParenthesizedExpression"
  ) {
    return assignmentTargetNames(node.expression);
  }
  return [];
}

function loopAssignmentTargetNames(node) {
  if (node?.type === "VariableDeclaration") {
    return [];
  }
  return assignmentTargetNames(node);
}

function topLevelProgramChild(path) {
  let current = path;
  while (current.parentPath && !current.parentPath.isProgram()) {
    current = current.parentPath;
  }
  return current.parentPath?.isProgram() ? current : null;
}

function addBindingDependency(sourceOwner, targetOwner, bindingName) {
  if (!sourceOwner.dependencies.has(targetOwner.id)) {
    sourceOwner.dependencies.set(targetOwner.id, new Set());
  }
  sourceOwner.dependencies.get(targetOwner.id).add(bindingName);
}

function buildExecutableSplitPlan(analysis) {
  const ownerById = new Map(analysis.owners.map((owner) => [owner.id, owner]));
  const unsafeReasons = new Map();
  let safeIds = new Set();

  for (const owner of analysis.owners) {
    const reason = initialUnsafeReason(owner);
    if (reason) {
      unsafeReasons.set(owner.id, reason);
      continue;
    }
    safeIds.add(owner.id);
  }

  let changed = true;
  while (changed) {
    changed = false;
    for (const ownerId of [...safeIds]) {
      const owner = ownerById.get(ownerId);
      const unsafeDependencies = [...owner.dependencies.keys()].filter((targetId) => !safeIds.has(targetId));
      if (unsafeDependencies.length === 0) {
        continue;
      }
      safeIds.delete(ownerId);
      unsafeReasons.set(
        ownerId,
        `depends_on_unsplit_top_level_binding:${unsafeDependencies
          .flatMap((targetId) => ownerById.get(targetId)?.names ?? [targetId])
          .sort()
          .join(",")}`
      );
      changed = true;
    }
  }

  const safeEdges = new Map([...safeIds].map((ownerId) => [ownerId, new Set()]));
  for (const ownerId of safeIds) {
    const owner = ownerById.get(ownerId);
    for (const targetId of owner.dependencies.keys()) {
      if (safeIds.has(targetId)) {
        safeEdges.get(ownerId).add(targetId);
      }
    }
  }

  const components = stronglyConnectedComponents([...safeIds], safeEdges).sort(
    (left, right) => minOwnerOrdinal(left, ownerById) - minOwnerOrdinal(right, ownerById)
  );

  const partByOwner = new Map();
  components.forEach((component, index) => {
    for (const ownerId of component) {
      partByOwner.set(ownerId, index);
    }
  });

  const parts = components.map((component, index) => buildPartRecord(component, index, ownerById, partByOwner));
  const partByBinding = new Map();
  for (const part of parts) {
    for (const name of part.exportedNames) {
      partByBinding.set(name, part);
    }
  }

  return {
    partByBinding,
    partByOwner,
    parts,
    splitOwnerIds: safeIds,
    unsafeReasons,
  };
}

function initialUnsafeReason(owner) {
  if (owner.type !== "FunctionDeclaration") {
    return `unsupported_top_level_declaration:${owner.type}`;
  }
  if (!owner.node.id) {
    return "anonymous_function_declaration";
  }
  if (functionContainsRuntimeSensitiveSyntax(owner.node)) {
    return "contains_import_meta_or_direct_eval";
  }
  return null;
}

function functionContainsRuntimeSensitiveSyntax(node) {
  let unsafe = false;
  traverse(t.file(t.program([t.cloneNode(node, true)])), {
    MetaProperty(path) {
      if (path.node.meta.name === "import" && path.node.property.name === "meta") {
        unsafe = true;
        path.stop();
      }
    },
    CallExpression(path) {
      if (
        path.node.callee.type === "Identifier" &&
        path.node.callee.name === "eval" &&
        !path.scope.hasBinding("eval")
      ) {
        unsafe = true;
        path.stop();
      }
    },
  });
  return unsafe;
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

function minOwnerOrdinal(component, ownerById) {
  return Math.min(...component.map((ownerId) => ownerById.get(ownerId).ordinal));
}

function buildPartRecord(component, index, ownerById, partByOwner) {
  const owners = component.map((ownerId) => ownerById.get(ownerId)).sort((left, right) => left.ordinal - right.ordinal);
  const externalImports = new Map();
  const importedParts = new Map();

  for (const owner of owners) {
    for (const importRecord of owner.externalImports.values()) {
      externalImports.set(importRecord.local, importRecord);
    }
    for (const [targetOwnerId, names] of owner.dependencies.entries()) {
      const targetPart = partByOwner.get(targetOwnerId);
      if (targetPart === undefined || targetPart === index) {
        continue;
      }
      if (!importedParts.has(targetPart)) {
        importedParts.set(targetPart, new Set());
      }
      for (const name of names) {
        importedParts.get(targetPart).add(name);
      }
    }
  }

  return {
    componentIndex: index,
    externalImports: [...externalImports.values()].sort(compareImports),
    file: `parts/part-${(index + 1).toString().padStart(4, "0")}.js`,
    importedParts,
    owners,
    ownerIds: owners.map((owner) => owner.id),
    exportedNames: owners.flatMap((owner) => owner.names).sort(),
  };
}

function compareImports(left, right) {
  return `${left.source}:${left.kind}:${left.local}`.localeCompare(`${right.source}:${right.kind}:${right.local}`);
}

function buildEntryFile(ast, plan, rewriteEntryImportSource) {
  const file = t.cloneNode(ast, true);
  transformRuntimeSources(file, rewriteEntryImportSource);

  const splitFunctionNames = new Set([...plan.partByBinding.keys()]);
  file.program.body = [
    ...runtimePartImports(plan.parts),
    ...file.program.body.filter((node) => !isSplitFunctionDeclaration(node, splitFunctionNames)),
  ];

  return {
    ast: file,
    headerLines: [
      "// Generated by //devinfra/js/debundle/split:split_chunk.",
      plan.parts.length > 0
        ? "// Executable chunk entry. Some safe top-level functions are imported from ./parts/."
        : "// Executable chunk entry.",
    ],
  };
}

function runtimePartImports(parts) {
  return parts
    .filter((part) => part.exportedNames.length > 0)
    .map((part) => importDeclarationForBindings(part.exportedNames, `./${part.file}`));
}

function isSplitFunctionDeclaration(node, splitFunctionNames) {
  return node.type === "FunctionDeclaration" && node.id && splitFunctionNames.has(node.id.name);
}

function buildPartFile(part, plan, rewriteEntryImportSource) {
  const body = [];
  const partImportSource = (source) => deepenRelativeImportSource(rewriteEntryImportSource(source));

  for (const importRecord of part.externalImports) {
    body.push(
      importDeclarationFromRecord({ source: importRecord.source, specifiers: [importRecord] }, partImportSource)
    );
  }

  for (const [componentIndex, names] of [...part.importedParts.entries()].sort((left, right) => left[0] - right[0])) {
    body.push(
      importDeclarationForBindings([...names].sort(), `./part-${(componentIndex + 1).toString().padStart(4, "0")}.js`)
    );
  }

  for (const owner of part.owners) {
    body.push(transformClonedStatement(owner.node, partImportSource));
  }

  if (part.exportedNames.length > 0) {
    body.push(exportDeclarationForBindings(part.exportedNames));
  }

  return {
    ast: t.file(t.program(body)),
    headerLines: [
      "// Generated by //devinfra/js/debundle/split:split_chunk.",
      `// Executable split part ${part.componentIndex + 1}; original owners: ${part.ownerIds.join(", ")}.`,
    ],
  };
}

export function serializeGeneratedJsFile(file) {
  return [...(file.headerLines ?? []), "", generate(file.ast, { comments: true }).code, ""].join("\n");
}

function deepenRelativeImportSource(source) {
  if (!source.startsWith(".")) {
    return source;
  }
  let rewritten = posix.normalize(posix.join("..", source));
  if (!rewritten.startsWith(".")) {
    rewritten = `./${rewritten}`;
  }
  return rewritten;
}

export function transformRuntimeSources(file, rewriteImportSource) {
  traverse(file, {
    ImportDeclaration(path) {
      rewriteStringLiteralSource(path.node.source, rewriteImportSource);
    },
    ExportNamedDeclaration(path) {
      rewriteStringLiteralSource(path.node.source, rewriteImportSource);
    },
    ExportAllDeclaration(path) {
      rewriteStringLiteralSource(path.node.source, rewriteImportSource);
    },
    CallExpression(path) {
      if (path.node.callee.type !== "Import") {
        return;
      }
      rewriteDynamicImportArgument(path.node.arguments[0], rewriteImportSource);
    },
    ImportExpression(path) {
      rewriteDynamicImportArgument(path.node.source, rewriteImportSource);
    },
    NewExpression(path) {
      rewriteWorkerConstructorArgument(path, rewriteImportSource);
    },
  });
}

function transformClonedStatement(node, rewriteImportSource) {
  const file = t.file(t.program([t.cloneNode(node, true)]));
  transformRuntimeSources(file, rewriteImportSource);
  return file.program.body[0];
}

function rewriteStringLiteralSource(source, rewriteImportSource) {
  if (t.isStringLiteral(source)) {
    source.value = rewriteImportSource(source.value);
  }
}

function rewriteDynamicImportArgument(argument, rewriteImportSource) {
  if (t.isStringLiteral(argument)) {
    argument.value = rewriteImportSource(argument.value);
  }
}

function rewriteWorkerConstructorArgument(path, rewriteImportSource) {
  if (path.node.callee.type !== "Identifier") {
    return;
  }
  if (path.node.callee.name !== "Worker" && path.node.callee.name !== "SharedWorker") {
    return;
  }
  if (path.scope.hasBinding(path.node.callee.name)) {
    return;
  }

  const [scriptArgument] = path.node.arguments;
  if (!t.isStringLiteral(scriptArgument)) {
    return;
  }

  const rewrittenSource = rewriteImportSource(scriptArgument.value);
  if (rewrittenSource === scriptArgument.value) {
    return;
  }

  path.node.arguments[0] = t.newExpression(t.identifier("URL"), [
    t.stringLiteral(rewrittenSource),
    t.memberExpression(t.metaProperty(t.identifier("import"), t.identifier("meta")), t.identifier("url")),
  ]);
}

function importDeclarationFromRecord(importRecord, rewriteImportSource = identity) {
  const source = rewriteImportSource(importRecord.source);
  return t.importDeclaration(importRecord.specifiers.map(importSpecifierFromRecord), t.stringLiteral(source));
}

function importSpecifierFromRecord(specifier) {
  if (specifier.kind === "default") {
    return t.importDefaultSpecifier(t.identifier(specifier.local));
  }
  if (specifier.kind === "namespace") {
    return t.importNamespaceSpecifier(t.identifier(specifier.local));
  }
  return t.importSpecifier(t.identifier(specifier.local), moduleExportName(specifier.imported));
}

function importDeclarationForBindings(names, source) {
  return t.importDeclaration(
    names.map((name) => t.importSpecifier(t.identifier(name), moduleExportName(name))),
    t.stringLiteral(source)
  );
}

function exportDeclarationForBindings(names) {
  return t.exportNamedDeclaration(
    null,
    names.map((name) => t.exportSpecifier(t.identifier(name), moduleExportName(name)))
  );
}

function moduleExportName(name) {
  if (t.isValidIdentifier(name)) {
    return t.identifier(name);
  }
  return t.stringLiteral(name);
}

export function writeSplitOutput(result, outDir, { force = false } = {}) {
  if (existsSync(outDir)) {
    const entries = readdirSync(outDir);
    if (entries.length > 0 && !force) {
      throw new Error(`Output directory is not empty: ${outDir}. Pass --force to replace it.`);
    }
    if (force) {
      rmSync(outDir, { force: true, recursive: true });
    }
  }
  mkdirSync(outDir, { recursive: true });

  for (const [relativePath, content] of result.files.entries()) {
    const absolutePath = join(outDir, relativePath);
    mkdirSync(dirname(absolutePath), { recursive: true });
    writeFileSync(absolutePath, content);
  }
}
