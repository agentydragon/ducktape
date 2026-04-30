import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parse } from "@babel/parser";
import {
  createFile,
  createArtifact,
  setArtifactChunkManifest,
  setArtifactManifest,
  setArtifactVendorAnnotations,
} from "../common/artifact.mjs";
import {
  cloneDefaultParserOptions,
  DEFAULT_PARSER_OPTIONS,
  modulePackageJson,
  writeJsonFile,
  writeTextFile,
} from "../common/parser_options.mjs";

export const FIXTURE_UI_VERSION = "fixture";

export function createTempFixtureRoot(prefix) {
  return mkdtempSync(join(tmpdir(), prefix));
}

export function createWebFixtureRoots(prefix) {
  const root = createTempFixtureRoot(prefix);
  return {
    analysisRoot: join(root, "analysis"),
    appRoot: join(root, "app"),
    extractedRoot: join(root, "extracted"),
    outRoot: join(root, "out"),
    packagesRoot: join(root, "node_modules"),
    root,
    snapshotRoot: join(root, "snapshot"),
    sourceRoot: join(root, "source"),
    splitRoot: join(root, "split"),
    transformedRoot: join(root, "transformed"),
    vendorsRoot: join(root, "vendors"),
  };
}

function ensureDir(path) {
  mkdirSync(path, { recursive: true });
  return path;
}

export function readUtf8(path) {
  return readFileSync(path, "utf8");
}

function writeFiles(root, files) {
  for (const [relativePath, content] of Object.entries(files)) {
    writeTextFile(join(root, relativePath), content);
  }
}

function writeModulePackageJson(root) {
  writeJsonFile(join(root, "package.json"), modulePackageJson());
}

function writeJsListFile(path, files) {
  writeTextFile(path, `${files.join("\n")}\n`);
}

export function writeSnapshotFixture({
  assetSummary,
  extractedRoot,
  files = {},
  html = null,
  includePackageJson = true,
  jsFiles = [],
  snapshotRoot,
}) {
  if (includePackageJson) {
    writeModulePackageJson(snapshotRoot);
  }
  if (html !== null) {
    writeTextFile(join(snapshotRoot, "index.html"), html);
  }
  writeFiles(snapshotRoot, files);
  if (extractedRoot) {
    ensureDir(extractedRoot);
    if (jsFiles.length > 0) {
      writeJsListFile(join(extractedRoot, "js-files.txt"), jsFiles);
    }
    if (assetSummary) {
      writeJsonFile(join(extractedRoot, "asset-summary.json"), assetSummary);
    }
  }
}

export function writeChunkFixture({
  entryFile = "runtime.js",
  chunkId,
  manifest = {},
  parts = {},
  root,
  runtime = "",
}) {
  const chunkRoot = join(root, ...chunkId.split("/"));
  writeTextFile(join(chunkRoot, entryFile), runtime);
  for (const [file, content] of Object.entries(parts)) {
    writeTextFile(join(chunkRoot, file), content);
  }
  const manifestValue = {
    schemaVersion: 1,
    chunkId,
    parser: manifest.parser ?? cloneDefaultParserOptions(),
    entryFile,
    ...manifest,
    files: manifest.files ?? [
      { file: entryFile, role: "entry" },
      ...Object.keys(parts)
        .sort()
        .map((file) => ({ file, role: "module" })),
    ],
    parts:
      manifest.parts ??
      Object.keys(parts)
        .sort()
        .map((file) => ({ file })),
  };
  writeJsonFile(join(chunkRoot, "manifest.json"), manifestValue);
  return {
    chunkRoot,
    manifest: manifestValue,
  };
}

export function writeSnapshotManifest(root, { chunks, counts = {}, ...rest }) {
  const manifest = {
    schemaVersion: 1,
    counts: {
      chunks: chunks.length,
      ...counts,
    },
    chunks: chunks.map((chunk) => (typeof chunk === "string" ? { chunkId: chunk } : chunk)),
    ...rest,
  };
  writeJsonFile(join(root, "manifest.json"), manifest);
  return manifest;
}

export function parseModuleCode(code, parserOptions = DEFAULT_PARSER_OPTIONS) {
  return parse(code, parserOptions);
}

export function makePipelineChunk(chunkId, files, { manifest } = {}) {
  const parser = manifest?.parser ?? cloneDefaultParserOptions();
  const entryFile =
    manifest?.entryFile ??
    (Object.prototype.hasOwnProperty.call(files, "runtime.js") ? "runtime.js" : Object.keys(files).sort()[0]);
  const normalizedManifest = {
    schemaVersion: 1,
    chunkId,
    parser,
    entryFile,
    ...(manifest ?? {}),
  };
  if (!normalizedManifest.files) {
    normalizedManifest.files = Object.keys(files)
      .sort()
      .map((file) => ({
        file,
        role: file === entryFile ? "entry" : "module",
      }));
  }
  if (!normalizedManifest.parts) {
    normalizedManifest.parts = Object.keys(files)
      .filter((file) => file !== entryFile)
      .sort()
      .map((file) => ({ file }));
  }

  const artifactFiles = Object.entries(files).map(([name, value]) => {
    const fileValue =
      typeof value === "string"
        ? {
            ast: parseModuleCode(value, parser),
          }
        : value;
    return createFile({
      path: name,
      ...(fileValue.ast ? { ast: fileValue.ast } : {}),
      ...(fileValue.content ? { content: fileValue.content } : {}),
      ...(fileValue.headerLines ? { headerLines: fileValue.headerLines } : {}),
      parserOptions: fileValue.parserOptions ?? parser,
      metadata: {
        chunkId,
        chunkFile: name,
        role: name === entryFile ? "entry" : "module",
      },
    });
  });

  return {
    chunkId,
    entryFile,
    files: artifactFiles,
    manifest: normalizedManifest,
  };
}

export function makePipelineArtifact(chunks, { annotations, manifest = {} } = {}) {
  const artifact = createArtifact({
    chunks: chunks.map((chunk) => ({
      chunkId: chunk.chunkId,
      entryFile: chunk.entryFile ?? chunk.manifest?.entryFile,
      files: chunk.files,
      ...(chunk.metadata ? { metadata: chunk.metadata } : {}),
    })),
  });
  const snapshotManifest = {
    schemaVersion: 1,
    chunks: chunks.map((chunk) => ({ chunkId: chunk.chunkId })),
    ...manifest,
  };
  setArtifactManifest(artifact, snapshotManifest);
  for (const chunk of chunks) {
    setArtifactChunkManifest(artifact, chunk.chunkId, chunk.manifest);
  }
  if (annotations?.vendor) {
    setArtifactVendorAnnotations(
      artifact,
      annotations.vendor instanceof Map ? annotations.vendor : new Map(annotations.vendor)
    );
  }
  return artifact;
}

export function runNodeScript(path) {
  const result = spawnSync(process.execPath, [path], {
    encoding: "utf8",
  });
  return {
    signal: result.signal,
    status: result.status,
    stderr: result.stderr,
    stdout: result.stdout,
  };
}

export function writeRunnableFixture(root, { entry = "original.js", files = {} }) {
  writeModulePackageJson(root);
  for (const [relativePath, content] of Object.entries(files)) {
    writeTextFile(join(root, relativePath), content);
  }
  return join(root, entry);
}

export function listJsFiles(root, prefix = "") {
  const files = [];
  for (const entry of readdirSync(join(root, prefix), { withFileTypes: true })) {
    const relativePath = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) {
      files.push(...listJsFiles(root, relativePath));
    } else if (entry.isFile() && entry.name.endsWith(".js")) {
      files.push(relativePath);
    }
  }
  return files;
}
