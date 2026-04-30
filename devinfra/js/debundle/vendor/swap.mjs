import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import generateModule from "@babel/generator";
import { parse } from "@babel/parser";
import * as t from "@babel/types";
import { DEFAULT_PARSER_OPTIONS, writeJsonFile } from "../common/parser_options.mjs";
import {
  deleteArtifactChunkManifest,
  getArtifactManifest,
  getArtifactVendorAnnotations,
  getChunkEntryFile,
  getChunkEntryPath,
  listChunkIds,
  listChunkFiles,
  removeFiles,
  requirePipelineArtifact,
  setArtifactManifest,
  setArtifactVendorAnnotations,
} from "../common/artifact.mjs";
import { readInstalledPackageMetadata, resolvePackageSubpath } from "../common/package_tree.mjs";
import { resolveWorkspacePath } from "../common/io.mjs";
import { resolveImportToChunkId } from "./exports.mjs";

const generate = generateModule.default ?? generateModule;
export function swapVendorChunks({
  artifact,
  operations,
  operationCatalog,
  packageRoots,
  packagesRoot,
  outputManifestPath,
  outputWrapperDir,
  write = true,
}) {
  requirePipelineArtifact(artifact, "swapVendorChunks");
  const catalog = operations ?? operationCatalog ?? [];
  const ops = catalog.filter((op) => op?.operation === "mark_vendor" && op.level === "swap");
  const resolvedOutputManifestPath = outputManifestPath ? resolveWorkspacePath(outputManifestPath) : null;
  const resolvedOutputWrapperDir = outputWrapperDir ? resolveWorkspacePath(outputWrapperDir) : null;

  const snapshotManifest = getArtifactManifest(artifact);
  const resolutions = {};
  const vendorAnnotations = getArtifactVendorAnnotations(artifact);
  const importAlignmentIndex = buildImportAlignmentIndex(artifact);

  for (const op of ops) {
    const chunkId = chunkIdFromChunkPath(op.chunkPath, op.id);
    const entryFile = getChunkEntryFile(artifact, chunkId);
    const entryRelativeFile = getChunkEntryPath(artifact, chunkId);
    if (!entryFile || !entryRelativeFile) {
      throw new Error(
        `swapVendorChunks operation ${op.id} targets missing chunk: chunkPath=${op.chunkPath} (chunkId=${chunkId})`
      );
    }
    if (!entryFile.ast) {
      throw new Error(`swapVendorChunks operation ${op.id} vendor chunk ${chunkId} is missing entry AST`);
    }

    // 1. Resolve upstream version from the Bazel-provided package tree.
    const installed = readInstalledPackageMetadata(op.package, { packageRoots, packagesRoot }).version;
    if (installed !== op.version) {
      throw new Error(
        `swapVendorChunks operation ${op.id} version mismatch for ${op.package}: op=${op.version}, installed=${installed}`
      );
    }

    // 2. Locate upstream file with containment guard.
    const upstreamPath = resolvePackageSubpath(op.package, op.subpath, { packageRoots, packagesRoot });

    const vendorExports = collectVendorExportedNames(entryFile.ast);
    const upstreamCode = readFileSync(upstreamPath, "utf8");

    let generatedWrapperPath = null;

    if (op.wrapperShape === "named-from-default") {
      const upstreamAst = parse(upstreamCode, DEFAULT_PARSER_OPTIONS);
      // 3a. Fan-out wrapper: upstream exports only `default` (an object), vendor
      //     re-exports its properties as named exports. Verify and generate a wrapper.
      const objectKeys = collectDefaultExportObjectKeys(upstreamAst, op);
      const nonDefaultVendorExports = new Set([...vendorExports].filter((n) => n !== "default"));
      const missingKeys = setDiff(nonDefaultVendorExports, objectKeys);
      if (missingKeys.size > 0) {
        throw new Error(
          `swapVendorChunks operation ${op.id} named-from-default wrapper shape mismatch for ${op.package}@${op.version}: ` +
            `vendor named exports missing from upstream default object keys=[${[...missingKeys].sort().join(",")}]`
        );
      }
      const wrapperSource = generateNamedFromDefaultWrapper(upstreamCode, [...nonDefaultVendorExports].sort(), op);
      const wrapperRelPath = `vendors/generated/${chunkId}/${entryRelativeFile}`;
      if (write && resolvedOutputWrapperDir) {
        const wrapperAbsPath = join(resolvedOutputWrapperDir, chunkId, ...entryRelativeFile.split("/"));
        mkdirSync(dirname(wrapperAbsPath), { recursive: true });
        writeFileSync(wrapperAbsPath, wrapperSource, "utf8");
      }
      generatedWrapperPath = wrapperRelPath;
    } else if (op.wrapperShape === "named-from-json-default") {
      const upstreamJson = parseUpstreamJson(upstreamCode, op);
      const nonDefaultVendorExports = new Set([...vendorExports].filter((n) => n !== "default"));
      const objectKeys = new Set(Object.keys(upstreamJson));
      const missingKeys = setDiff(nonDefaultVendorExports, objectKeys);
      if (missingKeys.size > 0) {
        throw new Error(
          `swapVendorChunks operation ${op.id} named-from-json-default wrapper shape mismatch for ${op.package}@${op.version}: ` +
            `vendor named exports missing from upstream JSON keys=[${[...missingKeys].sort().join(",")}]`
        );
      }
      const wrapperSource = generateNamedFromJsonDefaultWrapper(upstreamJson, [...nonDefaultVendorExports].sort());
      const wrapperRelPath = `vendors/generated/${chunkId}/${entryRelativeFile}`;
      if (write && resolvedOutputWrapperDir) {
        const wrapperAbsPath = join(resolvedOutputWrapperDir, chunkId, ...entryRelativeFile.split("/"));
        mkdirSync(dirname(wrapperAbsPath), { recursive: true });
        writeFileSync(wrapperAbsPath, wrapperSource, "utf8");
      }
      generatedWrapperPath = wrapperRelPath;
    } else if (op.wrapperShape === "named-from-module-default") {
      const upstreamAst = parse(upstreamCode, DEFAULT_PARSER_OPTIONS);
      const wrapperSource = generateNamedFromModuleDefaultWrapper(upstreamAst, [...vendorExports].sort(), op);
      const wrapperRelPath = `vendors/generated/${chunkId}/${entryRelativeFile}`;
      if (write && resolvedOutputWrapperDir) {
        const wrapperAbsPath = join(resolvedOutputWrapperDir, chunkId, ...entryRelativeFile.split("/"));
        mkdirSync(dirname(wrapperAbsPath), { recursive: true });
        writeFileSync(wrapperAbsPath, wrapperSource, "utf8");
      }
      generatedWrapperPath = wrapperRelPath;
    } else {
      const upstreamAst = parse(upstreamCode, DEFAULT_PARSER_OPTIONS);
      // 3b. Standard subset check: every vendor export must exist upstream.
      //     Upstream may export more (tree-shaking leaves vendor as a subset).
      const upstreamExports = collectUpstreamExportedNames(upstreamAst);
      const missing = setDiff(vendorExports, upstreamExports);
      if (missing.size > 0) {
        throw new Error(
          `swapVendorChunks operation ${op.id} export shape mismatch for ${op.package}@${op.version}: ` +
            `vendor exports not found upstream=[${[...missing].sort().join(",")}]`
        );
      }
    }

    // 4. Import-alignment: every static import from this vendor must use a real export name.
    for (const record of importAlignmentIndex.get(chunkId) ?? []) {
      for (const importedName of record.namedImports) {
        if (vendorExports.has(importedName)) {
          continue;
        }
        throw new Error(
          `swapVendorChunks operation ${op.id} import alignment failed: ` +
            `caller=${record.callerChunkId}/${record.callerFile} imports unknown specifier "${importedName}" ` +
            `from vendor ${chunkId} (known: [${[...vendorExports].sort().join(",")}])`
        );
      }
    }

    // 5. Remove + record.
    removeFiles(artifact, (file) => file.metadata?.chunkId === chunkId);
    deleteArtifactChunkManifest(artifact, chunkId);
    const priorAnnotation = vendorAnnotations.get(chunkId) ?? {};
    const swapEntry = {
      package: op.package,
      version: op.version,
      subpath: op.subpath,
      verification: "structural",
    };
    if (generatedWrapperPath != null) {
      swapEntry.wrapperShape = op.wrapperShape;
      swapEntry.generatedWrapperPath = generatedWrapperPath;
    }
    vendorAnnotations.set(chunkId, {
      ...priorAnnotation,
      chunkId,
      chunkPath: op.chunkPath,
      level: "swap",
      id: priorAnnotation.id ?? op.id,
      swap: swapEntry,
    });

    // 6. Resolutions.
    const resolutionEntry = {
      chunkId,
      chunkPath: op.chunkPath,
      entryFile: entryRelativeFile,
      package: op.package,
      version: op.version,
      subpath: op.subpath,
    };
    if (generatedWrapperPath != null) {
      resolutionEntry.wrapperShape = op.wrapperShape;
      resolutionEntry.generatedWrapperPath = generatedWrapperPath;
    }
    resolutions[op.chunkPath] = resolutionEntry;
  }

  setArtifactVendorAnnotations(artifact, vendorAnnotations);

  if (snapshotManifest) {
    const removedChunkIds = new Set(
      Object.keys(resolutions).map((chunkPath) => chunkIdFromChunkPath(chunkPath, "manifest"))
    );
    setArtifactManifest(artifact, {
      ...snapshotManifest,
      counts: {
        ...snapshotManifest.counts,
        chunks: listChunkIds(artifact).length,
      },
      chunks: snapshotManifest.chunks.filter((chunk) => !removedChunkIds.has(chunk.chunkId)),
    });
  }

  const manifest = {
    kind: "js.vendor_resolution_manifest",
    resolutions,
    counts: { swapped: Object.keys(resolutions).length },
  };

  if (write && resolvedOutputManifestPath) {
    mkdirSync(dirname(resolvedOutputManifestPath), { recursive: true });
    const payload = {
      kind: "js.vendor_resolution_manifest",
      resolutions,
    };
    writeJsonFile(resolvedOutputManifestPath, payload);
  }

  return {
    artifact,
    manifest,
    outputManifestPath: write && resolvedOutputManifestPath ? resolvedOutputManifestPath : null,
  };
}

function collectVendorExportedNames(ast) {
  const names = new Set();
  for (const node of ast.program.body) {
    if (!t.isExportNamedDeclaration(node)) {
      continue;
    }
    if (node.declaration) {
      for (const name of declaredNames(node.declaration)) {
        names.add(name);
      }
      continue;
    }
    for (const specifier of node.specifiers ?? []) {
      if (!t.isExportSpecifier(specifier)) {
        continue;
      }
      const exportedName = t.isIdentifier(specifier.exported)
        ? specifier.exported.name
        : t.isStringLiteral(specifier.exported)
          ? specifier.exported.value
          : null;
      if (exportedName) {
        names.add(exportedName);
      }
    }
  }
  return names;
}

function collectUpstreamExportedNames(ast) {
  const names = new Set();
  for (const node of ast.program.body) {
    if (t.isExportDefaultDeclaration(node)) {
      names.add("default");
      continue;
    }
    if (t.isExportNamedDeclaration(node)) {
      if (node.declaration) {
        for (const name of declaredNames(node.declaration)) {
          names.add(name);
        }
      }
      for (const specifier of node.specifiers ?? []) {
        if (t.isExportSpecifier(specifier)) {
          const exportedName = t.isIdentifier(specifier.exported)
            ? specifier.exported.name
            : t.isStringLiteral(specifier.exported)
              ? specifier.exported.value
              : null;
          if (exportedName) {
            names.add(exportedName);
          }
        }
      }
    }
  }
  return names;
}

function declaredNames(decl) {
  if (t.isFunctionDeclaration(decl) || t.isClassDeclaration(decl)) {
    return decl.id ? [decl.id.name] : [];
  }
  if (t.isVariableDeclaration(decl)) {
    const out = [];
    for (const declarator of decl.declarations) {
      collectPatternNames(declarator.id, out);
    }
    return out;
  }
  return [];
}

function collectPatternNames(node, out) {
  if (!node) {
    return;
  }
  if (t.isIdentifier(node)) {
    out.push(node.name);
    return;
  }
  if (t.isObjectPattern(node)) {
    for (const prop of node.properties) {
      if (t.isObjectProperty(prop)) {
        collectPatternNames(prop.value, out);
      } else if (t.isRestElement(prop)) {
        collectPatternNames(prop.argument, out);
      }
    }
    return;
  }
  if (t.isArrayPattern(node)) {
    for (const el of node.elements) {
      collectPatternNames(el, out);
    }
    return;
  }
  if (t.isAssignmentPattern(node)) {
    collectPatternNames(node.left, out);
  }
}

function setDiff(left, right) {
  const out = new Set();
  for (const value of left) {
    if (!right.has(value)) {
      out.add(value);
    }
  }
  return out;
}

function buildImportAlignmentIndex(artifact) {
  const index = new Map();
  for (const callerChunkId of listChunkIds(artifact)) {
    for (const fileArtifact of listChunkFiles(artifact, callerChunkId)) {
      const ast = fileArtifact.ast;
      if (!ast) {
        continue;
      }
      const callerFile = fileArtifact.path;
      for (const statement of ast.program.body) {
        if (!t.isImportDeclaration(statement)) {
          continue;
        }
        const targetChunkId = resolveImportToChunkId(artifact, statement.source?.value, callerChunkId, callerFile);
        if (!targetChunkId) {
          continue;
        }
        const namedImports = [];
        for (const specifier of statement.specifiers ?? []) {
          if (!t.isImportSpecifier(specifier)) {
            continue;
          }
          const imported = specifier.imported;
          const importedName = t.isIdentifier(imported)
            ? imported.name
            : t.isStringLiteral(imported)
              ? imported.value
              : null;
          if (importedName != null) {
            namedImports.push(importedName);
          }
        }
        if (namedImports.length === 0) {
          continue;
        }
        if (!index.has(targetChunkId)) {
          index.set(targetChunkId, []);
        }
        index.get(targetChunkId).push({
          callerChunkId,
          callerFile,
          namedImports,
        });
      }
    }
  }
  return index;
}

function chunkIdFromChunkPath(chunkPath, opId) {
  if (typeof chunkPath !== "string" || chunkPath === "") {
    throw new Error(`swapVendorChunks operation ${opId} has invalid chunkPath: ${chunkPath}`);
  }
  if (!chunkPath.endsWith(".js")) {
    throw new Error(`swapVendorChunks operation ${opId} chunkPath must end in .js: ${chunkPath}`);
  }
  return chunkPath.slice(0, -".js".length);
}

function collectDefaultExportObjectKeys(ast, op) {
  for (const node of ast.program.body) {
    if (!t.isExportDefaultDeclaration(node)) {
      continue;
    }
    const decl = node.declaration;
    if (!t.isObjectExpression(decl)) {
      throw new Error(
        `swapVendorChunks operation ${op.id} named-from-default: upstream default export is not an object literal ` +
          `(got ${decl.type})`
      );
    }
    const keys = new Set();
    for (const prop of decl.properties) {
      if (t.isObjectProperty(prop)) {
        const key = t.isIdentifier(prop.key) ? prop.key.name : t.isStringLiteral(prop.key) ? prop.key.value : null;
        if (key != null) {
          keys.add(key);
        }
      }
    }
    return keys;
  }
  throw new Error(`swapVendorChunks operation ${op.id} named-from-default: upstream has no export default declaration`);
}

function generateNamedFromDefaultWrapper(upstreamCode, namedExports, op) {
  const prefix = "export default ";
  if (!upstreamCode.startsWith(prefix)) {
    throw new Error(
      `swapVendorChunks operation ${op.id} named-from-default: upstream does not start with 'export default '`
    );
  }
  const body = upstreamCode.slice(prefix.length);
  const namedStmts = namedExports.map((n) => `export const ${n} = _d.${n};`).join("\n");
  return `const _d = ${body}\nexport default _d;\n${namedStmts}\n`;
}

function parseUpstreamJson(upstreamCode, op) {
  try {
    return JSON.parse(upstreamCode);
  } catch (error) {
    throw new Error(
      `swapVendorChunks operation ${op.id} named-from-json-default: upstream JSON parse failed: ${error.message}`
    );
  }
}

function generateNamedFromJsonDefaultWrapper(upstreamJson, namedExports) {
  const body = JSON.stringify(upstreamJson, null, 2);
  const namedStmts = namedExports.map((n) => `export const ${n} = _d.${n};`).join("\n");
  return `const _d = ${body};\nexport default _d;\n${namedStmts}\n`;
}

function generateNamedFromModuleDefaultWrapper(upstreamAst, vendorExports, op) {
  const program = t.cloneNode(upstreamAst.program, true);
  const defaultLocalName = "__vendor_default__";
  let foundDefault = false;
  const rewrittenBody = [];

  for (const node of program.body) {
    if (t.isExportDefaultDeclaration(node)) {
      foundDefault = true;
      const decl = node.declaration;
      if ((t.isFunctionDeclaration(decl) || t.isClassDeclaration(decl)) && decl.id) {
        rewrittenBody.push(decl);
        rewrittenBody.push(
          t.variableDeclaration("const", [
            t.variableDeclarator(t.identifier(defaultLocalName), t.identifier(decl.id.name)),
          ])
        );
      } else {
        rewrittenBody.push(
          t.variableDeclaration("const", [t.variableDeclarator(t.identifier(defaultLocalName), decl)])
        );
      }
      continue;
    }

    if (t.isExportNamedDeclaration(node) && !node.source && Array.isArray(node.specifiers)) {
      const remainingSpecifiers = [];
      let defaultSpecifierLocal = null;
      for (const specifier of node.specifiers) {
        if (!t.isExportSpecifier(specifier)) {
          remainingSpecifiers.push(specifier);
          continue;
        }
        const exportedName = t.isIdentifier(specifier.exported)
          ? specifier.exported.name
          : t.isStringLiteral(specifier.exported)
            ? specifier.exported.value
            : null;
        if (exportedName === "default") {
          foundDefault = true;
          defaultSpecifierLocal = specifier.local.name;
          continue;
        }
        remainingSpecifiers.push(specifier);
      }
      if (remainingSpecifiers.length > 0 || node.declaration) {
        rewrittenBody.push(
          t.exportNamedDeclaration(
            node.declaration,
            remainingSpecifiers,
            node.source,
            node.assertions ?? node.attributes ?? []
          )
        );
      }
      if (defaultSpecifierLocal) {
        rewrittenBody.push(
          t.variableDeclaration("const", [
            t.variableDeclarator(t.identifier(defaultLocalName), t.identifier(defaultSpecifierLocal)),
          ])
        );
      }
      continue;
    }

    rewrittenBody.push(node);
  }

  if (!foundDefault) {
    throw new Error(`swapVendorChunks operation ${op.id} named-from-module-default: upstream has no default export`);
  }

  rewrittenBody.push(t.exportDefaultDeclaration(t.identifier(defaultLocalName)));
  for (const exportName of vendorExports) {
    if (exportName === "default") {
      continue;
    }
    rewrittenBody.push(
      t.exportNamedDeclaration(
        t.variableDeclaration("const", [t.variableDeclarator(t.identifier(exportName), t.identifier(defaultLocalName))])
      )
    );
  }

  return `${generate({ ...program, body: rewrittenBody }, { comments: true }).code}\n`;
}
