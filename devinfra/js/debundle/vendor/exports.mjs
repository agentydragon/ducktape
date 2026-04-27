import { posix } from "node:path";
import * as t from "@babel/types";
import {
  getChunkEntryFile,
  getChunkEntryPath,
  listChunkIds,
  listChunkFiles,
  resolveArtifactImportReference,
  resolveArtifactSourceImportReference,
  requirePipelineArtifact,
} from "../common/artifact.mjs";

const RELEVANT_LEVELS = new Set(["boundary-rename", "swap"]);

export function renameVendorExports({ artifact, operations, operationCatalog }) {
  requirePipelineArtifact(artifact, "renameVendorExports");
  const catalog = operations ?? operationCatalog ?? [];
  const ops = catalog.filter(
    (op) => op?.operation === "mark_vendor" && RELEVANT_LEVELS.has(op.level)
  );

  let totalRewrites = 0;
  let chunksWithMapping = 0;
  const details = [];

  for (const op of ops) {
    const chunkId = chunkIdFromChunkPath(op.chunkPath, op.id);
    const vendorEntryFile = getChunkEntryFile(artifact, chunkId);
    const vendorEntryRelativeFile = getChunkEntryPath(artifact, chunkId);
    if (!vendorEntryFile || !vendorEntryRelativeFile) {
      throw new Error(
        `renameVendorExports operation ${op.id} targets missing chunk: chunkPath=${op.chunkPath} (chunkId=${chunkId})`
      );
    }
    if (!vendorEntryFile.ast) {
      throw new Error(
        `renameVendorExports operation ${op.id} vendor chunk ${chunkId} is missing entry AST`
      );
    }
    const mapping = collectBoundaryMapping(vendorEntryFile.ast);
    if (mapping.size === 0) {
      details.push({ opId: op.id, chunkId, mappingSize: 0, rewrites: 0, callers: [] });
      continue;
    }
    chunksWithMapping++;

    const callerCounts = new Map();
    let chunkRewrites = 0;
    for (const otherChunkId of listChunkIds(artifact)) {
      if (otherChunkId === chunkId) {
        continue;
      }
      for (const fileArtifact of listChunkFiles(artifact, otherChunkId)) {
        if (!fileArtifact.ast) {
          continue;
        }
        const file = fileArtifact.path;
        const callerFile = posix.join(otherChunkId, file);
        const rewrites = rewriteImportsInFile({
          artifact,
          ast: fileArtifact.ast,
          callerChunkId: otherChunkId,
          callerFile: file,
          targetEntryFile: vendorEntryRelativeFile,
          targetChunkId: chunkId,
          mapping,
          opId: op.id,
        });
        if (rewrites > 0) {
          chunkRewrites += rewrites;
          callerCounts.set(callerFile, (callerCounts.get(callerFile) ?? 0) + rewrites);
        }
      }
    }

    totalRewrites += chunkRewrites;
    details.push({
      opId: op.id,
      chunkId,
      mappingSize: mapping.size,
      rewrites: chunkRewrites,
      callers: [...callerCounts.entries()]
        .sort((left, right) => (left[0] < right[0] ? -1 : left[0] > right[0] ? 1 : 0))
        .map(([file, count]) => ({ file, rewrites: count })),
    });
  }

  return {
    artifact,
    manifest: {
      kind: "js.rename_vendor_exports_manifest",
      counts: {
        considered: ops.length,
        chunksWithMapping,
        rewrites: totalRewrites,
      },
      details,
    },
  };
}

function collectBoundaryMapping(ast) {
  // Map<localName, exportedName> for top-level `export { local as exported }` specifiers.
  const mapping = new Map();
  if (!Array.isArray(ast?.program?.body)) {
    return mapping;
  }
  for (const node of ast.program.body) {
    if (!t.isExportNamedDeclaration(node)) {
      continue;
    }
    if (node.declaration || !Array.isArray(node.specifiers) || node.specifiers.length === 0) {
      continue;
    }
    if (node.source) {
      // `export { a as b } from "./other"` — not a local-binding boundary.
      continue;
    }
    for (const specifier of node.specifiers) {
      if (!t.isExportSpecifier(specifier)) {
        continue;
      }
      const localName = specifier.local.name;
      const exportedName = t.isIdentifier(specifier.exported)
        ? specifier.exported.name
        : t.isStringLiteral(specifier.exported)
          ? specifier.exported.value
          : null;
      if (!exportedName || !t.isValidIdentifier(exportedName)) {
        continue;
      }
      if (localName === exportedName) {
        continue;
      }
      mapping.set(localName, exportedName);
    }
  }
  return mapping;
}

function rewriteImportsInFile({ artifact, ast, callerChunkId, callerFile, targetChunkId, targetEntryFile, mapping, opId }) {
  let rewrites = 0;
  if (!Array.isArray(ast?.program?.body)) {
    return 0;
  }
  for (const node of ast.program.body) {
    if (!t.isImportDeclaration(node)) {
      continue;
    }
    const source = node.source?.value;
    if (typeof source !== "string") {
      continue;
    }
    const resolved =
      resolveArtifactImportReference(artifact, source, { callerChunkId, callerFile }) ??
      resolveArtifactSourceImportReference(artifact, source, { callerChunkId, callerFile });
    if (!resolved || resolved.chunkId !== targetChunkId || resolved.file !== targetEntryFile) {
      continue;
    }
    for (const specifier of node.specifiers ?? []) {
      if (!t.isImportSpecifier(specifier)) {
        continue;
      }
      const imported = specifier.imported;
      const importedName = t.isIdentifier(imported)
        ? imported.name
        : t.isStringLiteral(imported)
          ? imported.value
          : null;
      if (importedName == null) {
        continue;
      }
      const mapped = mapping.get(importedName);
      if (mapped === undefined || mapped === importedName) {
        continue;
      }
      if (t.isIdentifier(imported)) {
        specifier.imported = t.identifier(mapped);
      } else {
        specifier.imported = t.stringLiteral(mapped);
      }
      rewrites++;
    }
  }
  return rewrites;
}

export function resolveImportToChunkId(artifact, source, callerChunkId, callerFile) {
  return (
    resolveArtifactImportReference(artifact, source, { callerChunkId, callerFile })?.chunkId ??
    resolveArtifactSourceImportReference(artifact, source, { callerChunkId, callerFile })?.chunkId ??
    null
  );
}

function chunkIdFromChunkPath(chunkPath, opId) {
  if (typeof chunkPath !== "string" || chunkPath === "") {
    throw new Error(`renameVendorExports operation ${opId} has invalid chunkPath: ${chunkPath}`);
  }
  if (!chunkPath.endsWith(".js")) {
    throw new Error(`renameVendorExports operation ${opId} chunkPath must end in .js: ${chunkPath}`);
  }
  return chunkPath.slice(0, -".js".length);
}
