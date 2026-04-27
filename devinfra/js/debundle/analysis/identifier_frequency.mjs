import { readFileSync } from "node:fs";
import { join } from "node:path";
import { parse } from "@babel/parser";
import traverseModule from "@babel/traverse";
import * as t from "@babel/types";
import { DEFAULT_PARSER_OPTIONS, writeJsonFile } from "../common/parser_options.mjs";
import {
  getArtifactChunkManifestOrDerived,
  getArtifactManifestChunks,
  getArtifactManifestOrDerived,
  requireChunkFile,
  getChunkEntryFile,
  getChunkEntryPath,
  listChunkFilePaths,
  getArtifactVendorAnnotations,
  requirePipelineArtifact,
} from "../common/artifact.mjs";
import {
  ensureOutputDir,
  formatDurationSince,
  logProgress,
  relativeWorkspacePath,
  resolveWorkspacePath,
} from "../common/io.mjs";

const traverse = traverseModule.default ?? traverseModule;

const EXCLUDED_IDENTIFIERS = new Set([
  "AbortController",
  "AbortSignal",
  "Array",
  "BigInt",
  "Blob",
  "Boolean",
  "Date",
  "Element",
  "Error",
  "Event",
  "File",
  "FormData",
  "Headers",
  "HTMLElement",
  "Infinity",
  "Intl",
  "JSON",
  "KeyboardEvent",
  "Map",
  "Math",
  "MouseEvent",
  "NaN",
  "Node",
  "Number",
  "Object",
  "Promise",
  "Proxy",
  "Reflect",
  "RegExp",
  "Request",
  "Response",
  "Set",
  "SharedWorker",
  "String",
  "Symbol",
  "TypeError",
  "URL",
  "URLSearchParams",
  "WeakMap",
  "WeakSet",
  "WebSocket",
  "Worker",
  "arguments",
  "cancelAnimationFrame",
  "clearInterval",
  "clearTimeout",
  "console",
  "crypto",
  "customElements",
  "document",
  "fetch",
  "history",
  "indexedDB",
  "localStorage",
  "location",
  "navigator",
  "performance",
  "queueMicrotask",
  "requestAnimationFrame",
  "sessionStorage",
  "setInterval",
  "setTimeout",
  "undefined",
  "window",
]);

export function extractScrambledIdentifierFrequencies(options) {
  const artifact = requirePipelineArtifact(options.artifact, "extractScrambledIdentifierFrequencies");
  const inputRoot = options.inputRoot ? resolveWorkspacePath(options.inputRoot) : null;
  const inputManifestPath = options.inputManifestPath ? resolveWorkspacePath(options.inputManifestPath) : null;
  const outDir = resolveWorkspacePath(options.outDir);
  const limit = options.limit ?? 200;

  ensureOutputDir(outDir);

  const splitManifest = getArtifactManifestOrDerived(artifact);
  const excludedIdentifiers = buildExcludedIdentifiers(splitManifest, options);
  const excludedSymbolFiles = buildExcludedSymbolFiles(options, artifact);
  const files = enumerateArtifactJsFiles(artifact, { excludedSymbolFiles });
  const aggregate = createAggregate();
  logProgress(`analysis start files=${files.length} mode=pipeline out=${relativeWorkspacePath(outDir)}`);
  const startedAt = process.hrtime.bigint();

  for (const file of files) {
    analyzeFile(file, aggregate, { excludedIdentifiers });
  }

  const symbols = [...aggregate.bySymbol.values()].map(finalizeSymbolRecord).sort(compareSymbolRecords).slice(0, limit);

  const report = buildReport({
    aggregate,
    excludedIdentifiers,
    excludedSymbolFiles,
    files,
    inputRoot,
    options,
    outDir,
    splitManifest,
    inputManifestPath,
    symbols,
  });

  const jsonPath = join(outDir, "scrambled-identifiers.json");
  writeJsonFile(jsonPath, report);
  logProgress(
    `analysis done files=${files.length} symbols=${report.counts.topLevelScrambledSymbols} duration=${formatDurationSince(
      startedAt
    )}`
  );
  return {
    artifact,
    manifest: report,
  };
}

function buildReport({
  aggregate,
  excludedIdentifiers,
  excludedSymbolFiles,
  files,
  inputRoot,
  inputManifestPath,
  options,
  outDir,
  splitManifest,
  symbols,
}) {
  return {
    schemaVersion: 2,
    inputRoot: inputRoot ? relativeWorkspacePath(inputRoot) : null,
    inputManifestPath: inputManifestPath ? relativeWorkspacePath(inputManifestPath) : null,
    outDir: relativeWorkspacePath(outDir),
    heuristic: {
      description:
        "Counts only top-level Babel bindings and their resolved references that still look minified: 1-3 chars, short names with $, _, digits, or short mixed-case generated names. Known platform globals, configured names, prior rename targets, and excluded vendored chunks are omitted.",
      builtInExcludedIdentifiers: [...EXCLUDED_IDENTIFIERS].sort(),
      configuredExcludedIdentifiers: [...(options.excludedIdentifiers ?? [])].sort(),
      excludedSymbolFiles: [...excludedSymbolFiles].sort(),
      renameTargetIdentifiers: renameTargetIdentifiers(splitManifest),
      excludedIdentifiers: [...excludedIdentifiers].sort(),
    },
    counts: {
      files: files.length,
      totalIdentifierOccurrences: aggregate.totalIdentifierOccurrences,
      scrambledIdentifierOccurrences: aggregate.scrambledIdentifierOccurrences,
      topLevelScrambledSymbolOccurrences: aggregate.topLevelScrambledSymbolOccurrences,
      topLevelScrambledSymbols: aggregate.bySymbol.size,
      emittedSymbols: symbols.length,
    },
    symbols,
  };
}

function enumerateArtifactJsFiles(artifact, { excludedSymbolFiles = new Set() } = {}) {
  const files = [];
  for (const chunk of getArtifactManifestChunks(artifact)) {
    if (isExcludedSymbolChunk(chunk, excludedSymbolFiles)) {
      continue;
    }
    const chunkManifest = getArtifactChunkManifestOrDerived(artifact, chunk.chunkId);
    const parserOptions = chunkManifest.parser ?? DEFAULT_PARSER_OPTIONS;
    const entryFile = getChunkEntryPath(artifact, chunk.chunkId);

    for (const file of listChunkFilePaths(artifact, chunk.chunkId)) {
      files.push({
        ast: requireArtifactAst(artifact, chunk.chunkId, file),
        chunkId: chunk.chunkId,
        parserOptions,
        role: file === entryFile ? "entry" : "module",
        relativePath: `${chunk.chunkId}/${file}`,
      });
    }
  }
  return files;
}

function requireArtifactAst(artifact, chunkId, file) {
  const fileArtifact = requireChunkFile(artifact, chunkId, file, "extractScrambledIdentifierFrequencies");
  if (!fileArtifact?.ast) {
    throw new Error(`Artifact snapshot is missing AST for ${chunkId}/${file}`);
  }
  return fileArtifact.ast;
}

function createAggregate() {
  return {
    bySymbol: new Map(),
    scrambledIdentifierOccurrences: 0,
    topLevelScrambledSymbolOccurrences: 0,
    totalIdentifierOccurrences: 0,
  };
}

function buildExcludedIdentifiers(splitManifest, options) {
  return new Set([
    ...EXCLUDED_IDENTIFIERS,
    ...(options.excludedIdentifiers ?? []),
    ...renameTargetIdentifiers(splitManifest),
  ]);
}

function buildExcludedSymbolFiles(options, artifact = null) {
  const out = new Set((options.excludedSymbolFiles ?? []).map(normalizeExcludedSymbolFile));
  for (const path of vendorAnnotatedChunkPaths(artifact)) {
    out.add(normalizeExcludedSymbolFile(path));
  }
  return out;
}

function vendorAnnotatedChunkPaths(artifact) {
  const annotations = artifact ? getArtifactVendorAnnotations(artifact) : null;
  if (!annotations) {
    return [];
  }
  const paths = [];
  for (const annotation of annotations.values()) {
    if (typeof annotation?.chunkPath === "string" && annotation.chunkPath !== "") {
      paths.push(annotation.chunkPath);
    }
  }
  return paths;
}

function normalizeExcludedSymbolFile(value) {
  if (typeof value !== "string" || value === "") {
    throw new Error(`Expected excluded symbol file to be a non-empty string, got: ${value}`);
  }
  return value.split("\\").join("/").replace(/^\.\//, "");
}

function isExcludedSymbolChunk(chunk, excludedSymbolFiles) {
  if (excludedSymbolFiles.size === 0) {
    return false;
  }
  const chunkId = chunk.chunkId;
  const candidates = new Set([chunkId, `${chunkId}.js`]);
  if (chunk.inputPath) {
    candidates.add(normalizeExcludedSymbolFile(chunk.inputPath));
  }
  for (const excluded of excludedSymbolFiles) {
    for (const candidate of candidates) {
      if (candidate === excluded || candidate.endsWith(`/${excluded}`)) {
        return true;
      }
    }
  }
  return false;
}

function renameTargetIdentifiers(splitManifest) {
  return [
    ...new Set(
      (splitManifest.renames ?? [])
        .map((rename) => rename?.to)
        .filter((name) => typeof name === "string" && name !== "")
    ),
  ].sort();
}

function analyzeFile(file, aggregate, { excludedIdentifiers }) {
  const ast = file.ast ?? parse(readFileSync(file.path, "utf8"), file.parserOptions ?? DEFAULT_PARSER_OPTIONS);

  traverse(ast, {
    Identifier(path) {
      const role = identifierRole(path);
      if (!role) {
        return;
      }

      aggregate.totalIdentifierOccurrences++;
      const name = path.node.name;
      if (!isScrambledIdentifier(name, excludedIdentifiers)) {
        return;
      }

      aggregate.scrambledIdentifierOccurrences++;
      const occurrence = {
        chunkId: file.chunkId,
        file: file.relativePath,
        line: path.node.loc?.start.line ?? null,
        name,
        role,
      };
      const binding = identifierBinding(path, name);
      if (isTopLevelBinding(binding)) {
        aggregate.topLevelScrambledSymbolOccurrences++;
        recordSymbol(aggregate.bySymbol, occurrence, symbolDescriptor(file, binding));
      }
    },
  });
}

function identifierRole(path) {
  if (path.isReferencedIdentifier()) {
    return "reference";
  }
  if (isBindingIdentifierPath(path)) {
    return "binding";
  }
  return null;
}

function isBindingIdentifierPath(path) {
  if (typeof path.isBindingIdentifier === "function" && path.isBindingIdentifier()) {
    return true;
  }

  const parent = path.parentPath;
  if (!parent) {
    return false;
  }
  if (
    (parent.isClassDeclaration() ||
      parent.isClassExpression() ||
      parent.isFunctionDeclaration() ||
      parent.isFunctionExpression()) &&
    path.key === "id"
  ) {
    return true;
  }
  if (
    (parent.isFunctionDeclaration() ||
      parent.isFunctionExpression() ||
      parent.isArrowFunctionExpression() ||
      parent.isObjectMethod() ||
      parent.isClassMethod()) &&
    path.listKey === "params"
  ) {
    return true;
  }
  if (parent.isVariableDeclarator() && path.key === "id") {
    return true;
  }
  if (parent.isCatchClause() && path.key === "param") {
    return true;
  }
  if (
    (parent.isImportSpecifier() || parent.isImportDefaultSpecifier() || parent.isImportNamespaceSpecifier()) &&
    path.key === "local"
  ) {
    return true;
  }
  if (parent.isRestElement() && path.key === "argument") {
    return true;
  }
  if (parent.isAssignmentPattern() && path.key === "left") {
    return true;
  }
  if (parent.isObjectProperty() && parent.parentPath?.isObjectPattern() && path.key === "value") {
    return true;
  }
  return false;
}

function identifierBinding(path, name) {
  const programPath = path.findParent((parent) => parent.isProgram());
  return path.scope.getBinding(name) ?? programPath?.scope.getBinding(name) ?? null;
}

function isTopLevelBinding(binding) {
  return binding?.scope.path.isProgram() ?? false;
}

export function isScrambledIdentifier(name, excludedIdentifiers = EXCLUDED_IDENTIFIERS) {
  if (!t.isValidIdentifier(name) || excludedIdentifiers.has(name) || name.startsWith("__")) {
    return false;
  }
  if (name.length <= 3) {
    return true;
  }
  if (name.length <= 6 && /[$_]/.test(name)) {
    return true;
  }
  if (name.length <= 5 && /\d/.test(name)) {
    return true;
  }
  if (name.length <= 5 && /[a-z]/.test(name) && /[A-Z]/.test(name)) {
    return true;
  }
  return false;
}

function recordSymbol(records, occurrence, symbol) {
  let record = records.get(symbol.id);
  if (!record) {
    record = {
      bindings: 0,
      chunks: new Map(),
      examples: [],
      files: new Map(),
      references: 0,
      symbol,
    };
    records.set(symbol.id, record);
  }

  if (occurrence.role === "binding") {
    record.bindings++;
  } else {
    record.references++;
  }
  incrementMap(record.files, occurrence.file);
  incrementMap(record.chunks, occurrence.chunkId);
  if (record.examples.length < 8) {
    record.examples.push({
      file: occurrence.file,
      line: occurrence.line,
      role: occurrence.role,
    });
  }
}

function incrementMap(map, key) {
  map.set(key, (map.get(key) ?? 0) + 1);
}

function finalizeSymbolRecord(record) {
  return {
    id: record.symbol.id,
    name: record.symbol.name,
    kind: record.symbol.kind,
    declaration: record.symbol.declaration,
    bindings: record.bindings,
    references: record.references,
    files: record.files.size,
    chunks: record.chunks.size,
    topFiles: topEntries(record.files, 5).map(([file, mentions]) => ({ file, mentions })),
    examples: record.examples,
  };
}

function topEntries(map, limit) {
  return [...map.entries()]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .slice(0, limit);
}

function compareSymbolRecords(left, right) {
  return (
    right.references - left.references ||
    right.bindings - left.bindings ||
    right.files - left.files ||
    left.name.localeCompare(right.name) ||
    left.id.localeCompare(right.id)
  );
}

function symbolDescriptor(file, binding) {
  const path = bindingDeclarationPath(binding);
  const line = path.node.loc?.start.line ?? null;
  const column = path.node.loc?.start.column ?? null;
  const kind = path.type;
  const descriptor = {
    declaration: {
      chunkId: file.chunkId,
      column,
      file: file.relativePath,
      kind,
      line,
    },
    id: `${file.relativePath}:${line ?? "unknown"}:${column ?? "unknown"}:${kind}:${binding.identifier.name}`,
    kind,
    name: binding.identifier.name,
  };
  if (path.isImportSpecifier() || path.isImportDefaultSpecifier() || path.isImportNamespaceSpecifier()) {
    descriptor.declaration.import = {
      imported: importSpecifierImportedName(path),
      source: path.parentPath.node.source.value,
    };
  }
  return descriptor;
}

function bindingDeclarationPath(binding) {
  const path = binding.path;
  if (
    path.isClassDeclaration() ||
    path.isFunctionDeclaration() ||
    path.isVariableDeclarator() ||
    path.isImportSpecifier() ||
    path.isImportDefaultSpecifier() ||
    path.isImportNamespaceSpecifier()
  ) {
    return path;
  }
  if (path.isIdentifier()) {
    const parentPath = path.parentPath;
    if (
      parentPath?.isClassDeclaration() ||
      parentPath?.isFunctionDeclaration() ||
      parentPath?.isVariableDeclarator() ||
      parentPath?.isImportSpecifier() ||
      parentPath?.isImportDefaultSpecifier() ||
      parentPath?.isImportNamespaceSpecifier()
    ) {
      return parentPath;
    }
  }
  return path;
}

function importSpecifierImportedName(path) {
  if (path.isImportDefaultSpecifier()) {
    return "default";
  }
  if (path.isImportNamespaceSpecifier()) {
    return "*";
  }
  const imported = path.node.imported;
  if (t.isIdentifier(imported)) {
    return imported.name;
  }
  if (t.isStringLiteral(imported)) {
    return imported.value;
  }
  return null;
}
