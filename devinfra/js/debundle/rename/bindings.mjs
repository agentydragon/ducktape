import { readFileSync } from "node:fs";
import { posix } from "node:path";
import generateModule from "@babel/generator";
import { parse } from "@babel/parser";
import traverseModule from "@babel/traverse";
import * as t from "@babel/types";
import { DEFAULT_PARSER_OPTIONS } from "../common/parser_options.mjs";
import {
  getArtifactManifestOrDerived,
  getArtifactChunkManifest,
  getArtifactChunkManifestOrDerived,
  getChunkEntryPath,
  requireChunkFile,
  requirePipelineArtifact,
  setArtifactChunkManifest,
  setArtifactManifest,
} from "../common/artifact.mjs";
import {
  formatDuration,
  formatDurationSince,
  logProgress,
} from "../common/io.mjs";

const generate = generateModule.default ?? generateModule;
const traverse = traverseModule.default ?? traverseModule;

export function renameBindingsInArtifact(options) {
  const artifact = requirePipelineArtifact(options.artifact, "renameBindingsInArtifact");
  const operations = loadOperations(options);
  const renameOperations = operations.filter((operation) => operation.operation === "rename_binding");
  logProgress(`rename start operations=${renameOperations.length} mode=pipeline`);
  const startedAt = process.hrtime.bigint();

  const applied = [];
  const targetTimings = [];
  const grouped = groupOperationsByTarget(renameOperations, artifact);
  const unmatchedTargets = new Set(grouped.keys());
  for (const [targetRelativePath, targetOperations] of grouped.entries()) {
    const targetStartedAt = process.hrtime.bigint();
    const chunkId = targetOperations[0].selector.chunkId;
    const file = resolveTargetFile(targetOperations[0], artifact);
    const fileArtifact = requireChunkFile(artifact, chunkId, file, "renameBindingsInArtifact");
    if (!fileArtifact.ast) {
      throw new Error(`renameBindingsInArtifact requires AST for file: ${targetRelativePath}`);
    }
    const chunkManifest = getArtifactChunkManifest(artifact, chunkId);
    const derivedChunkManifest = getArtifactChunkManifestOrDerived(artifact, chunkId);
    const result = renameBindingsInAst(fileArtifact.ast, targetOperations, {
      chunkManifest,
      file,
    });
    const updatedChunkManifest = {
      ...(derivedChunkManifest ?? {}),
      renames: mergeAppliedRecords(derivedChunkManifest?.renames, result.applied),
    };
    setArtifactChunkManifest(artifact, chunkId, updatedChunkManifest);
    applied.push(...result.applied);
    targetTimings.push({
      operations: targetOperations.length,
      targetRelativePath,
      durationMs: Number(process.hrtime.bigint() - targetStartedAt) / 1_000_000,
    });
    unmatchedTargets.delete(targetRelativePath);
  }

  if (unmatchedTargets.size > 0) {
    throw new Error(`Rename target file does not exist: ${[...unmatchedTargets].sort().join(", ")}`);
  }

  const manifest = getArtifactManifestOrDerived(artifact);
  const outputManifest = {
    ...manifest,
    counts: {
      ...manifest.counts,
      bindingRenames: applied.length,
    },
    renames: mergeAppliedRecords(manifest.renames, applied),
  };
  setArtifactManifest(artifact, outputManifest);
  logRenameDone({ applied, startedAt, targetTimings });
  return {
    artifact,
    manifest: outputManifest,
  };
}

function logRenameDone({ applied, startedAt, targetTimings }) {
  logProgress(`rename done applied=${applied.length} duration=${formatDurationSince(startedAt)}`);
  for (const target of [...targetTimings].sort((left, right) => right.durationMs - left.durationMs).slice(0, 8)) {
    logProgress(
      `rename slow-target target=${target.targetRelativePath} operations=${target.operations} duration=${formatDuration(
        target.durationMs
      )}`
    );
  }
}

function loadOperations(options) {
  if (Array.isArray(options.operations)) {
    return options.operations;
  }
  if (options.operationsPath) {
    return JSON.parse(readFileSync(options.operationsPath, "utf8"));
  }
  return [];
}

function groupOperationsByTarget(operations, artifact) {
  const grouped = new Map();
  for (const operation of operations) {
    validateRenameOperationShape(operation);
    const relativePath = targetRelativePath(operation, artifact);
    if (!grouped.has(relativePath)) {
      grouped.set(relativePath, []);
    }
    grouped.get(relativePath).push(operation);
  }
  return grouped;
}

function mergeAppliedRecords(existing, applied) {
  const merged = [...(existing ?? [])];
  const seenIds = new Set(merged.map((entry) => entry.id));
  for (const entry of applied) {
    if (seenIds.has(entry.id)) {
      continue;
    }
    merged.push(entry);
    seenIds.add(entry.id);
  }
  return merged;
}

function targetRelativePath(operation, artifact) {
  const chunkId = normalizeChunkId(operation.selector.chunkId);
  const file = normalizeRelativeFile(resolveTargetFile(operation, artifact));
  return posix.join(chunkId, file);
}

function resolveTargetFile(operation, artifact) {
  return operation.selector.file ?? getChunkEntryPath(artifact, operation.selector.chunkId);
}

function normalizeChunkId(value) {
  const normalized = normalizeRelativeFile(value);
  if (normalized.endsWith(".js")) {
    throw new Error(`Rename selector chunkId should not include .js: ${value}`);
  }
  return normalized;
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

function validateRenameOperationShape(operation) {
  if (!operation?.id) {
    throw new Error("Rename operation is missing id");
  }
  if (!operation.selector?.chunkId) {
    throw new Error(`Rename operation ${operation.id} is missing selector.chunkId`);
  }
  if (!operation.selector?.binding?.name) {
    throw new Error(`Rename operation ${operation.id} is missing selector.binding.name`);
  }
  if (!operation.target?.name) {
    throw new Error(`Rename operation ${operation.id} is missing target.name`);
  }
  validateIdentifier(operation.target.name, `target.name for ${operation.id}`);
}

export function renameBindingsInCode(code, operations, { chunkManifest, file } = {}) {
  const ast = parse(code, DEFAULT_PARSER_OPTIONS);
  const result = renameBindingsInAst(ast, operations, { chunkManifest, file });
  const parserOptions = chunkManifest?.parser ?? DEFAULT_PARSER_OPTIONS;

  return {
    applied: result.applied,
    code: serializeRenamedAst(ast, parserOptions),
  };
}

function renameBindingsInAst(ast, operations, { chunkManifest, file } = {}) {
  const resolvedFile = resolveRenameFile({ chunkManifest, file });
  const applied = [];

  let programPath;
  traverse(ast, {
    Program(path) {
      programPath = path;
      path.stop();
    },
  });
  if (!programPath) {
    throw new Error("Unable to locate Program path");
  }
  programPath.scope.crawl();
  const topLevelBindingIndex = buildTopLevelBindingIndex(programPath);
  const fingerprintCache = new WeakMap();

  const resolved = [];
  for (const operation of operations) {
    validateRenameOperationShape(operation);
    validateManifestOwner(operation, chunkManifest);
    const oldName = operation.selector.binding.name;
    const targetName = operation.target.name;
    validateIdentifier(oldName, `selector.binding.name for ${operation.id}`);
    const declarationPath = resolveUniqueBindingMatch(operation, {
      fingerprintCache,
      programPath,
      topLevelBindingIndex,
    });

    const binding = programPath.scope.getBinding(oldName);
    if (!binding || binding.scope !== programPath.scope) {
      throw new Error(`Rename operation ${operation.id} did not match a top-level binding named ${oldName}`);
    }
    const boundDeclarationPath = bindingDeclarationPath(binding);
    if (boundDeclarationPath.node !== declarationPath.node) {
      throw new Error(`Rename operation ${operation.id} matched multiple top-level bindings for ${oldName}`);
    }
    const actualKind = bindingKind(declarationPath);
    const expectedKind = operation.selector.binding.kind ?? "ClassDeclaration";
    if (actualKind !== expectedKind) {
      throw new Error(`Rename operation ${operation.id} expected ${expectedKind} for ${oldName}, got ${actualKind}`);
    }
    const preserveRuntimeName =
      declarationPath.isClassDeclaration() &&
      operation.preserveRuntimeName !== false &&
      !classDefinesStaticName(declarationPath.node);
    const preRenameFingerprint = cachedBindingFingerprint(declarationPath, fingerprintCache);
    assertFingerprint(operation, preRenameFingerprint);

    resolved.push({
      operation,
      binding,
      declarationPath,
      oldName,
      targetName,
      preserveRuntimeName,
      preRenameFingerprint,
    });
  }

  for (const entry of resolved) {
    const { operation, binding, declarationPath, oldName, targetName, preserveRuntimeName, preRenameFingerprint } =
      entry;

    if (programPath.scope.hasOwnBinding(targetName)) {
      throw new Error(`Rename operation ${operation.id} target ${targetName} already exists in program scope`);
    }
    if (preserveRuntimeName && programPath.scope.hasOwnBinding("Object")) {
      throw new Error(`Rename operation ${operation.id} cannot preserve runtime name because Object is shadowed`);
    }
    assertNoReferenceShadowing(operation, binding, targetName);

    renameBindingInProgramScope(programPath, binding, oldName, targetName);
    if (preserveRuntimeName) {
      declarationPath.insertAfter(runtimeNamePreservationStatement(targetName, oldName));
    }
    const renamedBinding = programPath.scope.getBinding(targetName);
    const renamedDeclarationPath = renamedBinding ? bindingDeclarationPath(renamedBinding) : null;
    if (!renamedBinding || renamedDeclarationPath?.node !== declarationPath.node) {
      throw new Error(`Rename operation ${operation.id} failed to create binding ${targetName}`);
    }
    if (programPath.scope.hasOwnBinding(oldName)) {
      throw new Error(`Rename operation ${operation.id} left old binding ${oldName} in program scope`);
    }

    applied.push({
      id: operation.id,
      operation: operation.operation,
      chunkId: operation.selector.chunkId,
      file: resolvedFile,
      from: oldName,
      to: targetName,
      preserveRuntimeName,
      owner: operation.selector.owner ?? null,
      fingerprint: preRenameFingerprint,
    });
  }

  return {
    applied,
    ast,
  };
}

function buildTopLevelBindingIndex(programPath) {
  const index = new Map();
  for (const statementPath of programPath.get("body")) {
    for (const candidatePath of topLevelBindingCandidates(statementPath)) {
      const name = bindingName(candidatePath);
      const kind = bindingKind(candidatePath);
      if (!name || !kind) {
        continue;
      }
      const key = topLevelBindingIndexKey(name, kind);
      if (!index.has(key)) {
        index.set(key, []);
      }
      index.get(key).push(candidatePath);
    }
  }
  return index;
}

function topLevelBindingIndexKey(name, kind) {
  return `${kind}:${name}`;
}

function resolveUniqueBindingMatch(operation, { fingerprintCache, programPath, topLevelBindingIndex }) {
  const oldName = operation.selector.binding.name;
  const expectedKind = operation.selector.binding.kind ?? "ClassDeclaration";
  const selectorOwner = operation.selector.owner ?? {};
  const candidates = (topLevelBindingIndex.get(topLevelBindingIndexKey(oldName, expectedKind)) ?? []).filter(
    (candidatePath) => selectorImportMatches(operation, candidatePath)
  );
  if (candidates.length === 1) {
    return candidates[0];
  }
  const lineMatchedCandidates = selectorOwner.line
    ? candidates.filter((candidatePath) => candidatePath.node.loc?.start.line === selectorOwner.line)
    : candidates;
  if (lineMatchedCandidates.length === 1) {
    return lineMatchedCandidates[0];
  }
  const fingerprintCandidates = lineMatchedCandidates.length > 0 ? lineMatchedCandidates : candidates;
  const matches = operation.fingerprint
    ? fingerprintCandidates.filter((candidatePath) =>
        fingerprintMatches(operation.fingerprint, cachedBindingFingerprint(candidatePath, fingerprintCache))
      )
    : fingerprintCandidates;
  if (matches.length !== 1) {
    throw new Error(`Rename operation ${operation.id} matched ${matches.length} top-level bindings for ${oldName}`);
  }
  return matches[0];
}

function renameBindingInProgramScope(programPath, binding, oldName, targetName) {
  const declarationPath = bindingDeclarationPath(binding);
  renameBindingIdentifierPath(declarationPath, targetName);
  for (const referencePath of binding.referencePaths) {
    renameReferencePath(referencePath, targetName);
  }
  for (const violationPath of binding.constantViolations) {
    for (const identifierPath of boundIdentifierPathsInside(violationPath, binding)) {
      renameReferencePath(identifierPath, targetName);
    }
  }

  delete programPath.scope.bindings[oldName];
  programPath.scope.bindings[targetName] = binding;
}

function renameBindingIdentifierPath(declarationPath, targetName) {
  if (declarationPath.isClassDeclaration() || declarationPath.isFunctionDeclaration()) {
    declarationPath.node.id.name = targetName;
    return;
  }
  if (
    declarationPath.isImportSpecifier() ||
    declarationPath.isImportDefaultSpecifier() ||
    declarationPath.isImportNamespaceSpecifier()
  ) {
    declarationPath.node.local.name = targetName;
    return;
  }
  if (declarationPath.isVariableDeclarator() && t.isIdentifier(declarationPath.node.id)) {
    declarationPath.node.id.name = targetName;
    return;
  }
  throw new Error(`Unsupported binding declaration path for rename: ${declarationPath?.type}`);
}

function renameReferencePath(referencePath, targetName) {
  if (!("name" in referencePath.node)) {
    throw new Error(`Unsupported reference path for rename: ${referencePath.type}`);
  }
  preserveObjectPropertyKeyIfNeeded(referencePath);
  referencePath.node.name = targetName;
}

function preserveObjectPropertyKeyIfNeeded(identifierPath) {
  const parentPath = identifierPath.parentPath;
  if (parentPath?.isObjectProperty() && parentPath.node.shorthand && parentPath.get("value") === identifierPath) {
    parentPath.node.shorthand = false;
  }
}

function boundIdentifierPathsInside(rootPath, binding) {
  const paths = [];
  if (rootPath.isIdentifier() && rootPath.scope.getBinding(rootPath.node.name) === binding) {
    paths.push(rootPath);
    return paths;
  }
  rootPath.traverse({
    Identifier(path) {
      if (path.scope.getBinding(path.node.name) === binding) {
        paths.push(path);
      }
    },
    JSXIdentifier(path) {
      if (path.scope.getBinding(path.node.name) === binding) {
        paths.push(path);
      }
    },
  });
  return paths;
}

function serializeRenamedAst(ast, parserOptions, { headerLines = [] } = {}) {
  const code = generate(ast, { comments: true }).code;
  const generated = headerLines.length > 0 ? [...headerLines, "", code, ""].join("\n") : `${code}\n`;
  parse(generated, parserOptions);
  return generated;
}

function validateIdentifier(name, label) {
  if (!t.isValidIdentifier(name)) {
    throw new Error(`Invalid JavaScript identifier for ${label}: ${name}`);
  }
}

function validateManifestOwner(operation, chunkManifest) {
  if (!chunkManifest) {
    return;
  }
  if (chunkManifest.chunkId !== operation.selector.chunkId) {
    throw new Error(
      `Rename operation ${operation.id} targets ${operation.selector.chunkId}, but manifest is ${chunkManifest.chunkId}`
    );
  }

  const kind = operation.selector.binding.kind ?? "ClassDeclaration";
  if (isImportKind(kind)) {
    validateManifestImport(operation, chunkManifest);
    return;
  }

  const selectorOwner = operation.selector.owner ?? {};
  const oldName = operation.selector.binding.name;
  const manifestKind = manifestDeclarationKind(kind);
  const candidates = (chunkManifest.keptTopLevelDeclarations ?? []).filter((owner) => {
    if (!owner.names?.includes(oldName) || owner.type !== manifestKind) {
      return false;
    }
    if (selectorOwner.id && owner.id !== selectorOwner.id) {
      return false;
    }
    if (selectorOwner.line && owner.line !== selectorOwner.line) {
      return false;
    }
    return true;
  });

  if (candidates.length !== 1) {
    throw new Error(`Rename operation ${operation.id} matched ${candidates.length} manifest owners for ${oldName}`);
  }
}

function validateManifestImport(operation, chunkManifest) {
  const selectorImport = operation.selector.import ?? operation.fingerprint ?? {};
  const oldName = operation.selector.binding.name;
  const file = resolveRenameFile({ chunkManifest, file: operation.selector.file });
  const candidates = manifestImportDeclarationsForFile(file, chunkManifest).flatMap((declaration) =>
    (declaration.specifiers ?? [])
      .filter((specifier) => specifier.local === oldName)
      .filter((specifier) => !selectorImport.kind || selectorImport.kind === specifier.kind)
      .filter((specifier) => !selectorImport.imported || selectorImport.imported === specifier.imported)
      .filter(() => !selectorImport.source || importSourcesEquivalent(selectorImport.source, declaration.source))
      .map((specifier) => ({ declaration, specifier }))
  );
  if (candidates.length !== 1) {
    throw new Error(`Rename operation ${operation.id} matched ${candidates.length} manifest imports for ${oldName}`);
  }
}

function manifestImportDeclarationsForFile(file, chunkManifest) {
  const declarations = [...(chunkManifest.imports ?? [])];
  if (file != null && file === chunkManifest.entryFile) {
    for (const part of chunkManifest.parts ?? []) {
      declarations.push({
        source: `./${part.file}`,
        specifiers: (part.exportedNames ?? []).map((name) => ({
          imported: name,
          kind: "named",
          local: name,
        })),
      });
    }
  }
  return declarations;
}

function resolveRenameFile({ chunkManifest, file } = {}) {
  if (typeof file === "string" && file !== "") {
    return file;
  }
  if (typeof chunkManifest?.entryFile === "string" && chunkManifest.entryFile !== "") {
    return chunkManifest.entryFile;
  }
  return null;
}

function manifestDeclarationKind(kind) {
  return kind === "VariableDeclarator" ? "VariableDeclaration" : kind;
}

function isImportKind(kind) {
  return kind === "ImportSpecifier" || kind === "ImportDefaultSpecifier" || kind === "ImportNamespaceSpecifier";
}

function assertNoReferenceShadowing(operation, binding, targetName) {
  for (const referencePath of binding.referencePaths) {
    let scope = referencePath.scope;
    while (scope && scope !== binding.scope) {
      if (scope.hasOwnBinding(targetName)) {
        throw new Error(
          `Rename operation ${operation.id} would shadow ${targetName} at line ${
            referencePath.node.loc?.start.line ?? "unknown"
          }`
        );
      }
      scope = scope.parent;
    }
  }
}

function assertFingerprint(operation, actual) {
  const expected = operation.fingerprint;
  if (!expected) {
    return;
  }
  if (fingerprintMatches(expected, actual)) {
    return;
  }
  if (expected.superClass !== undefined && expected.superClass !== actual.superClass) {
    throw new Error(
      `Rename operation ${operation.id} fingerprint superClass mismatch: expected ${expected.superClass}, got ${actual.superClass}`
    );
  }
  if (expected.memberNamesPrefix) {
    const actualPrefix = (actual.memberNames ?? []).slice(0, expected.memberNamesPrefix.length);
    if (!arrayEqual(actualPrefix, expected.memberNamesPrefix)) {
      throw new Error(
        `Rename operation ${operation.id} fingerprint members mismatch: expected ${JSON.stringify(
          expected.memberNamesPrefix
        )}, got ${JSON.stringify(actualPrefix)}`
      );
    }
  }
  if (expected.minMembers !== undefined && (actual.memberNames ?? []).length < expected.minMembers) {
    throw new Error(
      `Rename operation ${operation.id} fingerprint member count mismatch: expected at least ${expected.minMembers}, got ${
        (actual.memberNames ?? []).length
      }`
    );
  }
  throw new Error(
    `Rename operation ${operation.id} fingerprint mismatch: expected ${JSON.stringify(expected)}, got ${JSON.stringify(
      actual
    )}`
  );
}

function cachedBindingFingerprint(path, fingerprintCache) {
  const node = path.node;
  const cached = fingerprintCache.get(node);
  if (cached) {
    return cached;
  }
  const fingerprint = bindingFingerprint(path);
  fingerprintCache.set(node, fingerprint);
  return fingerprint;
}

function arrayEqual(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function fingerprintMatches(expected, actual) {
  if (expected.kind !== undefined && expected.kind !== actual.kind) {
    return false;
  }
  if (expected.name !== undefined && expected.name !== actual.name) {
    return false;
  }
  if (expected.source !== undefined && !importSourcesEquivalent(expected.source, actual.source)) {
    return false;
  }
  if (expected.imported !== undefined && expected.imported !== actual.imported) {
    return false;
  }
  if (expected.local !== undefined && expected.local !== actual.local) {
    return false;
  }
  if (expected.superClass !== undefined && expected.superClass !== actual.superClass) {
    return false;
  }
  if (expected.memberNamesPrefix) {
    const actualPrefix = (actual.memberNames ?? []).slice(0, expected.memberNamesPrefix.length);
    if (!arrayEqual(actualPrefix, expected.memberNamesPrefix)) {
      return false;
    }
  }
  if (expected.minMembers !== undefined && (actual.memberNames ?? []).length < expected.minMembers) {
    return false;
  }
  if (expected.initEquals !== undefined && normalizeFingerprintCode(expected.initEquals) !== normalizeFingerprintCode(actual.init)) {
    return false;
  }
  if (
    expected.initStartsWith !== undefined &&
    !normalizeFingerprintCode(actual.init).startsWith(normalizeFingerprintCode(expected.initStartsWith))
  ) {
    return false;
  }
  if (expected.objectPropertyNamesPrefix) {
    const actualPrefix = (actual.objectPropertyNames ?? []).slice(0, expected.objectPropertyNamesPrefix.length);
    if (!arrayEqual(actualPrefix, expected.objectPropertyNamesPrefix)) {
      return false;
    }
  }
  if (
    expected.minObjectProperties !== undefined &&
    (actual.objectPropertyNames ?? []).length < expected.minObjectProperties
  ) {
    return false;
  }
  if (expected.paramsCount !== undefined && expected.paramsCount !== actual.paramsCount) {
    return false;
  }
  if (
    expected.bodyContains !== undefined &&
    !normalizeFingerprintCode(actual.body).includes(normalizeFingerprintCode(expected.bodyContains))
  ) {
    return false;
  }
  return true;
}

function normalizeFingerprintCode(value) {
  if (typeof value !== "string") {
    return "";
  }
  return value.replaceAll("\r\n", "\n").replace(/\s+/g, " ").trim();
}

function runtimeNamePreservationStatement(targetName, oldName) {
  return t.expressionStatement(
    t.callExpression(t.memberExpression(t.identifier("Object"), t.identifier("defineProperty")), [
      t.identifier(targetName),
      t.stringLiteral("name"),
      t.objectExpression([
        t.objectProperty(t.identifier("value"), t.stringLiteral(oldName)),
        t.objectProperty(t.identifier("configurable"), t.booleanLiteral(true)),
      ]),
    ])
  );
}

function classDefinesStaticName(node) {
  return node.body.body.some((member) => member.static && !member.computed && memberKeyName(member) === "name");
}

function classFingerprint(node) {
  if (!t.isClassDeclaration(node)) {
    throw new Error(`Expected ClassDeclaration fingerprint input, got ${node?.type}`);
  }
  return {
    name: node.id?.name ?? null,
    superClass: node.superClass ? generate(node.superClass).code : null,
    memberNames: node.body.body.map(memberName),
  };
}

function bindingFingerprint(path) {
  if (path.isClassDeclaration()) {
    return {
      kind: "ClassDeclaration",
      ...classFingerprint(path.node),
    };
  }
  if (path.isFunctionDeclaration()) {
    return {
      body: generate(path.node.body).code,
      kind: "FunctionDeclaration",
      name: path.node.id?.name ?? null,
      paramsCount: path.node.params.length,
    };
  }
  if (path.isVariableDeclarator()) {
    return {
      init: path.node.init ? generate(path.node.init).code : null,
      kind: "VariableDeclarator",
      name: bindingName(path),
      objectPropertyNames: t.isObjectExpression(path.node.init) ? objectPropertyNames(path.node.init) : [],
    };
  }
  if (path.isImportSpecifier()) {
    return {
      imported: importSpecifierImportedName(path.node),
      kind: "ImportSpecifier",
      local: path.node.local.name,
      name: path.node.local.name,
      source: path.parentPath.node.source.value,
    };
  }
  if (path.isImportDefaultSpecifier()) {
    return {
      imported: "default",
      kind: "ImportDefaultSpecifier",
      local: path.node.local.name,
      name: path.node.local.name,
      source: path.parentPath.node.source.value,
    };
  }
  if (path.isImportNamespaceSpecifier()) {
    return {
      imported: "*",
      kind: "ImportNamespaceSpecifier",
      local: path.node.local.name,
      name: path.node.local.name,
      source: path.parentPath.node.source.value,
    };
  }
  throw new Error(`Unsupported binding fingerprint input: ${path?.type}`);
}

function topLevelBindingCandidates(statementPath) {
  if (statementPath.isImportDeclaration()) {
    return statementPath.get("specifiers");
  }
  if (statementPath.isClassDeclaration() || statementPath.isFunctionDeclaration()) {
    return [statementPath];
  }
  if (statementPath.isVariableDeclaration()) {
    return statementPath.get("declarations").filter((declarationPath) => t.isIdentifier(declarationPath.node.id));
  }
  return [];
}

function bindingDeclarationPath(binding) {
  const path = binding.path;
  if (
    path.isClassDeclaration() ||
    path.isFunctionDeclaration() ||
    path.isVariableDeclarator() ||
    isImportKind(path.type)
  ) {
    return path;
  }
  if (path.isIdentifier()) {
    const parentPath = path.parentPath;
    if (
      parentPath?.isClassDeclaration() ||
      parentPath?.isFunctionDeclaration() ||
      parentPath?.isVariableDeclarator() ||
      isImportKind(parentPath?.type)
    ) {
      return parentPath;
    }
  }
  throw new Error(`Unsupported binding path: ${path?.type}`);
}

function bindingKind(path) {
  if (!path) {
    return null;
  }
  return path.type;
}

function bindingName(path) {
  if (path.isClassDeclaration() || path.isFunctionDeclaration()) {
    return path.node.id?.name ?? null;
  }
  if (path.isVariableDeclarator()) {
    return t.isIdentifier(path.node.id) ? path.node.id.name : null;
  }
  if (isImportKind(path.type)) {
    return path.node.local.name;
  }
  return null;
}

function selectorImportMatches(operation, candidatePath) {
  if (!isImportKind(operation.selector.binding.kind ?? "ClassDeclaration")) {
    return true;
  }
  const selectorImport = operation.selector.import ?? {};
  const actualSource = candidatePath.parentPath.node.source.value;
  if (selectorImport.source && !importSourcesEquivalent(selectorImport.source, actualSource)) {
    return false;
  }
  if (selectorImport.imported && selectorImport.imported !== importedNameForImportBinding(candidatePath)) {
    return false;
  }
  if (selectorImport.kind && selectorImport.kind !== candidatePath.type) {
    return false;
  }
  return true;
}

function importedNameForImportBinding(path) {
  if (path.isImportSpecifier()) {
    return importSpecifierImportedName(path.node);
  }
  if (path.isImportDefaultSpecifier()) {
    return "default";
  }
  if (path.isImportNamespaceSpecifier()) {
    return "*";
  }
  return null;
}

function importSpecifierImportedName(node) {
  if (t.isIdentifier(node.imported)) {
    return node.imported.name;
  }
  if (t.isStringLiteral(node.imported)) {
    return node.imported.value;
  }
  return generate(node.imported).code;
}

function objectPropertyNames(node) {
  return node.properties.map((property) => {
    if (t.isSpreadElement(property)) {
      return `...${generate(property.argument).code}`;
    }
    return propertyKeyName(property);
  });
}

function propertyKeyName(property) {
  if (property.computed) {
    return `[${generate(property.key).code}]`;
  }
  if (t.isIdentifier(property.key)) {
    return property.key.name;
  }
  if (t.isStringLiteral(property.key) || t.isNumericLiteral(property.key)) {
    return String(property.key.value);
  }
  return generate(property.key).code;
}

function importSourcesEquivalent(left, right) {
  if (left === undefined || right === undefined) {
    return left === right;
  }
  if (left === right) {
    return true;
  }
  const leftChunk = importSourceChunkName(left);
  const rightChunk = importSourceChunkName(right);
  return leftChunk !== null && leftChunk === rightChunk;
}

function importSourceChunkName(source) {
  const normalized = source.split("\\").join("/");
  if (normalized.endsWith(".js")) {
    const parts = normalized.split("/");
    const stem = parts.at(-1)?.slice(0, -".js".length) ?? null;
    const parent = parts.at(-2) ?? null;
    if (parent && parent !== "." && parent !== ".." && stem && stem !== parent) {
      return parent;
    }
    return stem;
  }
  return null;
}

function memberName(member) {
  const prefixes = [];
  if (member.static) {
    prefixes.push("static");
  }
  if (member.kind === "get" || member.kind === "set") {
    prefixes.push(member.kind);
  }
  prefixes.push(memberKeyName(member));
  return prefixes.join(" ");
}

function memberKeyName(member) {
  if (member.kind === "constructor") {
    return "constructor";
  }
  if (member.computed) {
    return `[${generate(member.key).code}]`;
  }
  if (t.isIdentifier(member.key)) {
    return member.key.name;
  }
  if (t.isStringLiteral(member.key) || t.isNumericLiteral(member.key)) {
    return String(member.key.value);
  }
  if (t.isPrivateName(member.key)) {
    return `#${member.key.id.name}`;
  }
  return generate(member.key).code;
}
