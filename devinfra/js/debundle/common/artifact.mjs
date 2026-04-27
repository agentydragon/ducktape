import { posix } from "node:path";
import { cloneDefaultParserOptions } from "./parser_options.mjs";

const FILE_INDEX_CACHE = new WeakMap();
const SOURCE_CHUNK_INDEX_CACHE = new WeakMap();

export function createArtifact({ chunks = [], extras = {} } = {}) {
  const artifact = {
    kind: "js.pipeline_artifact",
    chunks: normalizeChunkMap(chunks),
    extras: normalizeArtifactExtras(extras),
  };
  primeArtifactIndexes(artifact);
  return artifact;
}

export function createEmptyArtifact() {
  return createArtifact();
}

function isPipelineArtifact(value) {
  return value?.kind === "js.pipeline_artifact" && value?.chunks instanceof Map;
}

export function requirePipelineArtifact(artifact, stageName) {
  if (!isPipelineArtifact(artifact)) {
    throw new Error(`${stageName} requires a js.pipeline_artifact`);
  }
  return artifact;
}

export function createChunk({ chunkId, entryFile = undefined, files = [], metadata = undefined } = {}) {
  return normalizePipelineChunk({
    chunkId,
    entryFile,
    files,
    metadata,
  });
}

export function getChunk(artifact, chunkId) {
  return requirePipelineArtifact(artifact, "getChunk").chunks.get(normalizeChunkId(chunkId)) ?? null;
}

function requireChunk(artifact, chunkId, stageName = "stage") {
  const chunk = getChunk(artifact, chunkId);
  if (!chunk) {
    throw new Error(`${stageName} missing artifact chunk: ${chunkId}`);
  }
  return chunk;
}

export function setChunk(artifact, chunk) {
  requirePipelineArtifact(artifact, "setChunk");
  const normalized = normalizePipelineChunk(chunk);
  artifact.chunks.set(normalized.chunkId, normalized);
  clearArtifactIndexes(artifact);
  return artifact;
}

function deleteChunk(artifact, chunkId) {
  requirePipelineArtifact(artifact, "deleteChunk");
  const deleted = artifact.chunks.delete(normalizeChunkId(chunkId));
  if (deleted) {
    clearArtifactIndexes(artifact);
  }
  return deleted;
}

export function replaceChunks(artifact, chunks) {
  requirePipelineArtifact(artifact, "replaceChunks");
  artifact.chunks = normalizeChunkMap(chunks);
  clearArtifactIndexes(artifact);
  return artifact;
}

export function createFile({
  path,
  content = undefined,
  ast = undefined,
  parserOptions = undefined,
  headerLines = undefined,
  metadata = undefined,
} = {}) {
  if (typeof path !== "string" || path === "") {
    throw new Error(`Expected a non-empty JS artifact path, got: ${path}`);
  }
  return normalizeJsArtifactFile({
    path,
    language: "js",
    ...(content !== undefined ? { content } : {}),
    ...(ast !== undefined ? { ast } : {}),
    ...(parserOptions !== undefined ? { parserOptions } : {}),
    ...(headerLines !== undefined ? { headerLines } : {}),
    ...(metadata !== undefined ? { metadata } : {}),
  });
}

export function listJsArtifactFiles(artifact) {
  requirePipelineArtifact(artifact, "listJsArtifactFiles");
  return listChunkIds(artifact).flatMap((chunkId) => listChunkFiles(artifact, chunkId));
}

function getJsArtifactFile(artifact, path) {
  const normalizedPath = normalizeArtifactFilePath(path);
  const index = fileIndex(artifact);
  return index.get(normalizedPath) ?? null;
}


export function getChunkFile(artifact, chunkId, file) {
  const chunk = getChunk(artifact, chunkId);
  if (!chunk) {
    return null;
  }
  return chunk.files.get(normalizeRelativePath(file)) ?? null;
}

export function requireChunkFile(artifact, chunkId, file, stageName = "stage") {
  const artifactFile = getChunkFile(artifact, chunkId, file);
  if (!artifactFile) {
    throw new Error(`${stageName} missing JS artifact file: ${serializeArtifactFilePath(chunkId, file)}`);
  }
  return artifactFile;
}

export function setFile(artifact, file) {
  requirePipelineArtifact(artifact, "setFile");
  const chunkId = normalizeChunkId(file?.metadata?.chunkId);
  const normalized = normalizeJsArtifactFile(file, { chunkId });
  const chunk = getChunk(artifact, chunkId) ?? createChunk({ chunkId });
  if (normalized.metadata?.role === "entry" || normalized.metadata?.role === "runtime") {
    chunk.entryFile = normalized.path;
  } else if (!chunk.entryFile) {
    chunk.entryFile = normalized.path;
  }
  chunk.files.set(normalized.path, normalized);
  artifact.chunks.set(chunkId, chunk);
  clearArtifactIndexes(artifact);
  return artifact;
}

function deleteJsArtifactFile(artifact, path) {
  requirePipelineArtifact(artifact, "deleteJsArtifactFile");
  const artifactFile = getJsArtifactFile(artifact, path);
  if (!artifactFile) {
    return false;
  }
  const chunkId = artifactChunkIdForFile(artifactFile);
  if (!chunkId) {
    return false;
  }
  const chunk = getChunk(artifact, chunkId);
  if (!chunk) {
    return false;
  }
  chunk.files.delete(artifactFile.path);
  if (chunk.files.size === 0) {
    artifact.chunks.delete(chunkId);
  } else if (chunk.entryFile === artifactFile.path) {
    chunk.entryFile = deriveChunkEntryFile(chunk);
  }
  clearArtifactIndexes(artifact);
  return true;
}

function replaceFiles(artifact, files) {
  requirePipelineArtifact(artifact, "replaceFiles");
  const grouped = new Map();
  for (const file of files) {
    const chunkId = normalizeChunkId(file?.metadata?.chunkId);
    if (!grouped.has(chunkId)) {
      grouped.set(chunkId, []);
    }
    grouped.get(chunkId).push(file);
  }
  artifact.chunks = normalizeChunkMap(
    [...grouped.entries()].map(([chunkId, chunkFiles]) => ({
      chunkId,
      files: chunkFiles,
    }))
  );
  clearArtifactIndexes(artifact);
  return artifact;
}

export function removeFiles(artifact, predicate) {
  requirePipelineArtifact(artifact, "removeFiles");
  const nextChunks = [];
  for (const chunkId of listChunkIds(artifact)) {
    const chunk = requireChunk(artifact, chunkId, "removeFiles");
    const keptFiles = listChunkFiles(artifact, chunkId).filter((file) => !predicate(file));
    if (keptFiles.length === 0) {
      continue;
    }
    nextChunks.push({
      chunkId,
      entryFile: keptFiles.some((file) => file.path === chunk.entryFile) ? chunk.entryFile : undefined,
      files: keptFiles,
      metadata: chunk.metadata,
    });
  }
  artifact.chunks = normalizeChunkMap(nextChunks);
  clearArtifactIndexes(artifact);
  return artifact;
}

export function listChunkIds(artifact) {
  return [...requirePipelineArtifact(artifact, "listChunkIds").chunks.keys()].sort();
}

export function listChunks(artifact) {
  return listChunkIds(artifact).map((chunkId) => {
    const chunk = requireChunk(artifact, chunkId, "listChunks");
    return {
      chunkId,
      entryFile: getChunkEntryPath(artifact, chunkId),
      files: listChunkFiles(artifact, chunkId),
      metadata: { ...(chunk.metadata ?? {}) },
    };
  });
}

export function listChunkFiles(artifact, chunkId) {
  const chunk = getChunk(artifact, chunkId);
  if (!chunk) {
    return [];
  }
  const entryFile = getChunkEntryPath(artifact, chunkId);
  return sortFilesWithEntryFirst([...chunk.files.keys()], entryFile).map((file) => chunk.files.get(file));
}

export function listChunkFilePaths(artifact, chunkId) {
  const chunk = getChunk(artifact, chunkId);
  if (!chunk) {
    return [];
  }
  return sortFilesWithEntryFirst([...chunk.files.keys()], getChunkEntryPath(artifact, chunkId));
}

export function getChunkEntryPath(artifact, chunkId) {
  const chunk = getChunk(artifact, chunkId);
  if (!chunk || chunk.files.size === 0) {
    return null;
  }

  if (chunk.entryFile && chunk.files.has(chunk.entryFile)) {
    return chunk.entryFile;
  }

  const manifest = getArtifactChunkManifest(artifact, chunkId);
  if (manifest?.entryFile) {
    const manifestEntryFile = normalizeRelativePath(manifest.entryFile);
    if (chunk.files.has(manifestEntryFile)) {
      return manifestEntryFile;
    }
  }

  const roleEntry = [...chunk.files.values()].find(
    (file) => file.metadata?.role === "entry" || file.metadata?.role === "runtime"
  );
  if (roleEntry) {
    return roleEntry.path;
  }

  return deriveChunkEntryFile(chunk);
}

export function getChunkEntryFile(artifact, chunkId) {
  const entryFile = getChunkEntryPath(artifact, chunkId);
  if (!entryFile) {
    return null;
  }
  return getChunkFile(artifact, chunkId, entryFile);
}

function getChunkSourcePath(artifact, chunkId) {
  const chunk = getChunk(artifact, chunkId);
  if (!chunk) {
    return null;
  }
  return normalizeOptionalSourceJsPath(
    getArtifactChunkManifest(artifact, chunkId)?.sourcePath ??
      chunk.metadata?.sourcePath ??
      getChunkEntryFile(artifact, chunkId)?.metadata?.sourcePath
  );
}

export function resolveArtifactImportReference(artifact, source, { callerChunkId, callerFile } = {}) {
  requirePipelineArtifact(artifact, "resolveArtifactImportReference");
  if (typeof source !== "string" || source === "" || typeof callerChunkId !== "string" || typeof callerFile !== "string") {
    return null;
  }
  if (!source.startsWith(".")) {
    return null;
  }
  const callerDir = posix.join(normalizeChunkId(callerChunkId), posix.dirname(normalizeRelativePath(callerFile)));
  const resolvedPath = posix.normalize(posix.join(callerDir, source));
  const targetFile = getJsArtifactFile(artifact, resolvedPath);
  if (!targetFile) {
    return null;
  }
  const targetChunkId = artifactChunkIdForFile(targetFile);
  if (!targetChunkId) {
    return null;
  }
  return {
    chunkId: targetChunkId,
    file: targetFile.path,
    path: resolvedPath,
  };
}

export function resolveArtifactSourceImportReference(artifact, source, { callerChunkId, callerFile } = {}) {
  requirePipelineArtifact(artifact, "resolveArtifactSourceImportReference");
  if (typeof source !== "string" || source === "" || typeof callerChunkId !== "string" || typeof callerFile !== "string") {
    return null;
  }
  if (!source.startsWith(".") && !source.startsWith("/")) {
    return null;
  }

  const callerSourcePath = sourcePathForArtifactFile(artifact, callerChunkId, callerFile);
  if (!callerSourcePath) {
    return null;
  }

  const importedSourcePath = resolveChunkSourcePathReference(source, callerSourcePath);
  if (!importedSourcePath) {
    return null;
  }

  const targetChunkId = sourceChunkIndex(artifact).get(importedSourcePath);
  if (!targetChunkId) {
    return null;
  }
  const targetEntryFile = getChunkEntryPath(artifact, targetChunkId);
  if (!targetEntryFile) {
    return null;
  }
  return {
    chunkId: targetChunkId,
    file: targetEntryFile,
    path: serializeArtifactFilePath(targetChunkId, targetEntryFile),
    sourcePath: importedSourcePath,
  };
}

function artifactChunkIdForFile(file) {
  return file.metadata?.chunkId ?? null;
}

export function ensureArtifactExtras(artifact) {
  requirePipelineArtifact(artifact, "ensureArtifactExtras");
  if (!artifact.extras) {
    artifact.extras = normalizeArtifactExtras({});
  }
  if (!artifact.extras.manifests) {
    artifact.extras.manifests = { root: null, chunks: new Map() };
  } else if (!(artifact.extras.manifests.chunks instanceof Map)) {
    artifact.extras.manifests.chunks = new Map(artifact.extras.manifests.chunks ?? []);
  }
  if (!artifact.extras.annotations) {
    artifact.extras.annotations = {};
  }
  return artifact.extras;
}

export function getArtifactManifest(artifact) {
  return ensureArtifactExtras(artifact).manifests.root ?? null;
}

export function getArtifactManifestChunks(artifact) {
  const manifest = getArtifactManifest(artifact);
  if (Array.isArray(manifest?.chunks)) {
    return manifest.chunks;
  }
  return listChunkIds(artifact).map((chunkId) => ({ chunkId }));
}

export function getArtifactManifestOrDerived(artifact) {
  const manifest = getArtifactManifest(artifact);
  if (manifest) {
    return manifest;
  }
  return {
    schemaVersion: 1,
    counts: {
      chunks: listChunkIds(artifact).length,
    },
    chunks: getArtifactManifestChunks(artifact),
  };
}

export function setArtifactManifest(artifact, manifest) {
  ensureArtifactExtras(artifact).manifests.root = manifest;
  return artifact;
}

export function getArtifactChunkManifest(artifact, chunkId) {
  return ensureArtifactExtras(artifact).manifests.chunks.get(normalizeChunkId(chunkId)) ?? null;
}

export function getArtifactChunkManifestOrDerived(artifact, chunkId) {
  const manifest = getArtifactChunkManifest(artifact, chunkId);
  const files = listChunkFiles(artifact, chunkId);
  if (files.length === 0) {
    return manifest ?? null;
  }

  const entryFile = getChunkEntryPath(artifact, chunkId);
  const parser =
    manifest?.parser ??
    files.find((file) => file.path === entryFile)?.parserOptions ??
    files[0]?.parserOptions ??
    cloneDefaultParserOptions();
  const fileRecords = sortFilesWithEntryFirst(
    files.map((file) => file.path),
    entryFile
  ).map((file) => ({
    file,
    role: file === entryFile ? "entry" : "module",
  }));

  return {
    schemaVersion: 1,
    chunkId: normalizeChunkId(chunkId),
    parser,
    ...(manifest ?? {}),
    entryFile,
    files: manifest?.files ?? fileRecords,
    parts: manifest?.parts ?? fileRecords.filter((file) => file.file !== entryFile).map((file) => ({ file: file.file })),
  };
}

export function setArtifactChunkManifest(artifact, chunkId, manifest) {
  const normalizedChunkId = normalizeChunkId(chunkId);
  ensureArtifactExtras(artifact).manifests.chunks.set(normalizedChunkId, manifest);
  const chunk = getChunk(artifact, normalizedChunkId);
  if (chunk && typeof manifest?.entryFile === "string") {
    const entryFile = normalizeRelativePath(manifest.entryFile);
    if (chunk.files.has(entryFile)) {
      chunk.entryFile = entryFile;
    }
  }
  clearArtifactIndexes(artifact);
  return artifact;
}

export function deleteArtifactChunkManifest(artifact, chunkId) {
  const deleted = ensureArtifactExtras(artifact).manifests.chunks.delete(normalizeChunkId(chunkId));
  if (deleted) {
    clearArtifactIndexes(artifact);
  }
  return deleted;
}


export function getArtifactVendorAnnotations(artifact) {
  const extras = ensureArtifactExtras(artifact);
  if (!(extras.annotations.vendor instanceof Map)) {
    extras.annotations.vendor = new Map(extras.annotations.vendor ?? []);
  }
  return extras.annotations.vendor;
}

export function setArtifactVendorAnnotations(artifact, annotations) {
  ensureArtifactExtras(artifact).annotations.vendor = annotations;
  return artifact;
}

function normalizeChunkMap(chunks) {
  const map = new Map();
  const entries =
    chunks instanceof Map
      ? [...chunks.values()]
      : Array.isArray(chunks)
        ? chunks
        : (() => {
            throw new Error("Expected artifact chunks to be an array or Map");
          })();
  for (const chunk of entries) {
    const normalized = normalizePipelineChunk(chunk);
    if (map.has(normalized.chunkId)) {
      throw new Error(`Duplicate artifact chunk: ${normalized.chunkId}`);
    }
    map.set(normalized.chunkId, normalized);
  }
  return map;
}

function normalizePipelineChunk(chunk) {
  if (!chunk || typeof chunk !== "object") {
    throw new Error(`Expected a chunk object, got: ${chunk}`);
  }
  const chunkId = normalizeChunkId(chunk.chunkId);
  const files = new Map();
  const fileEntries =
    chunk.files instanceof Map
      ? [...chunk.files.values()]
      : Array.isArray(chunk.files)
        ? chunk.files
        : [];
  for (const file of fileEntries) {
    const normalizedFile = normalizeJsArtifactFile(file, { chunkId });
    if (files.has(normalizedFile.path)) {
      throw new Error(`Duplicate artifact file in ${chunkId}: ${normalizedFile.path}`);
    }
    files.set(normalizedFile.path, normalizedFile);
  }
  const normalizedEntryFile = resolveChunkEntryFile({
    chunk,
    files,
  });
  return {
    chunkId,
    entryFile: normalizedEntryFile,
    files,
    metadata: { ...(chunk.metadata ?? {}) },
  };
}

function resolveChunkEntryFile({ chunk, files }) {
  if (typeof chunk.entryFile === "string" && chunk.entryFile !== "") {
    const explicit = normalizeRelativePath(chunk.entryFile);
    if (files.size > 0 && !files.has(explicit)) {
      throw new Error(`Chunk ${chunk.chunkId} entryFile does not exist: ${chunk.entryFile}`);
    }
    return explicit;
  }
  for (const file of files.values()) {
    if (file.metadata?.role === "entry" || file.metadata?.role === "runtime") {
      return file.path;
    }
  }
  return deriveChunkEntryFile({ files });
}

function deriveChunkEntryFile(chunk) {
  return [...chunk.files.keys()].sort()[0] ?? null;
}

function normalizeJsArtifactFile(file, { chunkId = undefined } = {}) {
  if (file?.language && file.language !== "js") {
    throw new Error(`Pipeline artifact only supports js files, got: ${file.language}`);
  }
  const path = normalizeRelativePath(file.path);
  const metadata = { ...(file.metadata ?? {}) };
  if (chunkId !== undefined) {
    if (metadata.chunkId !== undefined && normalizeChunkId(metadata.chunkId) !== chunkId) {
      throw new Error(`Artifact file ${path} has mismatched chunkId: ${metadata.chunkId} != ${chunkId}`);
    }
    metadata.chunkId = chunkId;
    metadata.chunkFile = path;
  } else if (metadata.chunkFile === undefined) {
    metadata.chunkFile = path;
  }
  return {
    path,
    language: "js",
    ...(file.content !== undefined ? { content: file.content } : {}),
    ...(file.ast !== undefined ? { ast: file.ast } : {}),
    ...(file.parserOptions !== undefined ? { parserOptions: file.parserOptions } : {}),
    ...(file.headerLines !== undefined ? { headerLines: [...file.headerLines] } : {}),
    metadata,
  };
}

function normalizeArtifactExtras(extras) {
  const manifests = extras.manifests
    ? {
        root: extras.manifests.root ?? extras.manifests.snapshot ?? null,
        chunks: extras.manifests.chunks instanceof Map ? extras.manifests.chunks : new Map(extras.manifests.chunks ?? []),
      }
    : {
        root: null,
        chunks: new Map(),
      };
  const annotations = { ...(extras.annotations ?? {}) };
  if (annotations.vendor && !(annotations.vendor instanceof Map)) {
    annotations.vendor = new Map(annotations.vendor);
  }
  return {
    ...extras,
    manifests,
    annotations,
  };
}

function fileIndex(artifact) {
  requirePipelineArtifact(artifact, "fileIndex");
  return FILE_INDEX_CACHE.get(artifact) ?? primeArtifactIndexes(artifact).fileIndex;
}

function sourceChunkIndex(artifact) {
  requirePipelineArtifact(artifact, "sourceChunkIndex");
  return SOURCE_CHUNK_INDEX_CACHE.get(artifact) ?? primeArtifactIndexes(artifact).sourceChunkIndex;
}

function primeArtifactIndexes(artifact) {
  const index = new Map();
  const sourceIndex = new Map();
  for (const [chunkId, chunk] of artifact.chunks.entries()) {
    for (const file of chunk.files.values()) {
      index.set(serializeArtifactFilePath(chunkId, file.path), file);
    }
    const sourcePath = normalizeOptionalSourceJsPath(
      artifact.extras?.manifests?.chunks?.get(chunkId)?.sourcePath ??
        chunk.metadata?.sourcePath ??
        chunk.files.get(chunk.entryFile ?? "")?.metadata?.sourcePath
    );
    if (sourcePath) {
      const existingChunkId = sourceIndex.get(sourcePath);
      if (existingChunkId && existingChunkId !== chunkId) {
        throw new Error(`Duplicate chunk sourcePath ${sourcePath}: ${existingChunkId} and ${chunkId}`);
      }
      sourceIndex.set(sourcePath, chunkId);
    }
  }
  FILE_INDEX_CACHE.set(artifact, index);
  SOURCE_CHUNK_INDEX_CACHE.set(artifact, sourceIndex);
  return { fileIndex: index, sourceChunkIndex: sourceIndex };
}

function clearArtifactIndexes(artifact) {
  FILE_INDEX_CACHE.delete(artifact);
  SOURCE_CHUNK_INDEX_CACHE.delete(artifact);
}

function normalizeChunkId(value) {
  return normalizeRelativePath(value);
}

function normalizeArtifactFilePath(value) {
  if (typeof value !== "string" || value === "") {
    throw new Error(`Expected a non-empty artifact file path, got: ${value}`);
  }
  const normalized = posix.normalize(value.split("\\").join("/"));
  if (normalized === "." || normalized.startsWith("/") || normalized.split("/").includes("..")) {
    throw new Error(`Invalid artifact file path: ${value}`);
  }
  return normalized;
}

function normalizeRelativePath(value) {
  if (typeof value !== "string" || value === "") {
    throw new Error(`Expected a non-empty relative path, got: ${value}`);
  }
  const normalized = posix.normalize(value.split("\\").join("/"));
  if (normalized === "." || normalized.startsWith("/") || normalized.split("/").includes("..")) {
    throw new Error(`Invalid relative path: ${value}`);
  }
  return normalized;
}

function normalizeOptionalSourceJsPath(value) {
  if (typeof value !== "string" || value === "") {
    return null;
  }
  const normalized = normalizeRelativePath(value);
  if (!normalized.endsWith(".js")) {
    throw new Error(`Expected a .js source path, got: ${value}`);
  }
  return normalized;
}

function sourcePathForArtifactFile(artifact, chunkId, file) {
  const artifactFile = getChunkFile(artifact, chunkId, file);
  return normalizeOptionalSourceJsPath(artifactFile?.metadata?.sourcePath ?? getChunkSourcePath(artifact, chunkId));
}

function resolveChunkSourcePathReference(source, callerSourcePath) {
  try {
    const importedPath = source.startsWith("/")
      ? normalizeRelativePath(source.slice(1))
      : normalizeRelativePath(posix.join(posix.dirname(callerSourcePath), source));
    return importedPath.endsWith(".js") ? importedPath : null;
  } catch {
    return null;
  }
}

function serializeArtifactFilePath(chunkId, file) {
  return posix.join(normalizeChunkId(chunkId), normalizeRelativePath(file));
}

function sortFilesWithEntryFirst(files, entryFile) {
  const sorted = [...files].sort();
  if (!entryFile) {
    return sorted;
  }
  const index = sorted.indexOf(entryFile);
  if (index <= 0) {
    return sorted;
  }
  sorted.splice(index, 1);
  sorted.unshift(entryFile);
  return sorted;
}
