// Pure program-level analysis: a single iteration over `ast.program.body`
// to identify imports, export aliases, top-level binding owners, and
// side-effect statements. Does not touch function bodies and does not need
// babel-traverse / scope info — see split/chunk.mjs for the full
// dependency-graph analysis that does.
//
// Shared by the metadata-only normalize pass and the split-into-parts pass;
// kept here so neither depends on internals of the other.

import { cloneDefaultParserOptions } from "./parser_options.mjs";

export const EMPTY_SPLIT_PLAN = Object.freeze({
  partByBinding: new Map(),
  partByOwner: new Map(),
  parts: [],
  splitOwnerIds: new Set(),
  unsafeReasons: new Map(),
});

export function analyzeProgramShallow(ast) {
  const imports = [];
  const importByLocalName = new Map();
  const exportAliases = [];
  const owners = [];
  const ownerByTopNode = new Map();
  const ownerByBinding = new Map();
  const sideEffects = [];
  const sideEffectByTopNode = new Map();

  ast.program.body.forEach((node, ordinal) => {
    if (node.type === "ImportDeclaration") {
      const importRecord = describeImport(node, imports.length);
      imports.push(importRecord);
      for (const specifier of importRecord.specifiers) {
        importByLocalName.set(specifier.local, { ...specifier, source: importRecord.source });
      }
      return;
    }

    if (node.type === "ExportNamedDeclaration" && node.specifiers.length > 0) {
      for (const specifier of node.specifiers) {
        exportAliases.push({
          exported: specifierName(specifier.exported),
          line: node.loc?.start.line ?? null,
          local: specifierName(specifier.local),
        });
      }
      return;
    }

    if (node.type === "ExportDefaultDeclaration") {
      exportAliases.push({
        exported: "default",
        line: node.loc?.start.line ?? null,
        local: node.declaration?.id?.name ?? null,
      });
      return;
    }

    const names = topLevelDeclarationNames(node);
    if (names.length > 0) {
      const owner = {
        dependencies: new Map(),
        externalImports: new Map(),
        id: `owner_${owners.length.toString().padStart(5, "0")}`,
        line: node.loc?.start.line ?? null,
        names,
        node,
        ordinal,
        type: node.type,
      };
      owners.push(owner);
      ownerByTopNode.set(node, owner);
      for (const name of names) {
        ownerByBinding.set(name, owner);
      }
      return;
    }

    const sideEffect = {
      dependencies: new Set(),
      externalImports: new Map(),
      id: `side_effect_${sideEffects.length.toString().padStart(5, "0")}`,
      line: node.loc?.start.line ?? null,
      node,
      ordinal,
      type: node.type,
    };
    sideEffects.push(sideEffect);
    sideEffectByTopNode.set(node, sideEffect);
  });

  return {
    dynamicImportCount: 0,
    exportAliases,
    importByLocalName,
    imports,
    ownerByBinding,
    ownerByTopNode,
    ownerEdges: new Map(owners.map((owner) => [owner.id, new Set()])),
    owners,
    sideEffectByTopNode,
    sideEffects,
  };
}

export function buildChunkManifestFromAnalysis(chunkId, entryFile, sourcePath, analysis, plan = EMPTY_SPLIT_PLAN) {
  const partByOwner = new Map();
  for (const part of plan.parts) {
    for (const owner of part.owners) {
      partByOwner.set(owner.id, part.file);
    }
  }

  const unresolvedExports = analysis.exportAliases.filter(
    (alias) => !analysis.ownerByBinding.has(alias.local) && !analysis.importByLocalName.has(alias.local)
  );
  const keptOwners = analysis.owners.filter((owner) => !plan.splitOwnerIds.has(owner.id));

  return {
    schemaVersion: 1,
    chunkId,
    sourcePath,
    parser: cloneDefaultParserOptions(),
    entryFile,
    counts: {
      dynamicImports: analysis.dynamicImportCount,
      exportAliases: analysis.exportAliases.length,
      importDeclarations: analysis.imports.length,
      keptTopLevelDeclarationOwners: keptOwners.length,
      parts: plan.parts.length,
      splitFunctionDeclarations: plan.splitOwnerIds.size,
      topLevelBindings: analysis.owners.reduce((count, owner) => count + owner.names.length, 0),
      topLevelDeclarationOwners: analysis.owners.length,
      topLevelSideEffects: analysis.sideEffects.length,
      unresolvedExports: unresolvedExports.length,
    },
    files: [{ file: entryFile, role: "entry" }, ...plan.parts.map((part) => ({ file: part.file, role: "module" }))],
    imports: analysis.imports,
    exportAliases: analysis.exportAliases,
    unresolvedExports,
    keptTopLevelDeclarations: keptOwners.map((owner) => ({
      id: owner.id,
      line: owner.line,
      names: owner.names,
      type: owner.type,
      unsafeReason: plan.unsafeReasons.get(owner.id) ?? "not_split",
    })),
    parts: plan.parts.map((part) => ({
      exportedNames: part.exportedNames,
      externalImports: part.externalImports.map((importRecord) => ({
        imported: importRecord.imported,
        kind: importRecord.kind,
        local: importRecord.local,
        source: importRecord.source,
      })),
      file: part.file,
      imports: [...part.importedParts.entries()].map(([componentIndex, names]) => ({
        file: plan.parts[componentIndex].file,
        names: [...names].sort(),
      })),
      owners: part.owners.map((owner) => ({
        id: owner.id,
        line: owner.line,
        names: owner.names,
        type: owner.type,
      })),
    })),
    ownerToPart: Object.fromEntries([...partByOwner.entries()].sort()),
  };
}

export function describeImport(node, index) {
  return {
    id: `import_${index.toString().padStart(5, "0")}`,
    line: node.loc?.start.line ?? null,
    source: node.source.value,
    specifiers: node.specifiers.map((specifier) => {
      if (specifier.type === "ImportDefaultSpecifier") {
        return { kind: "default", local: specifier.local.name };
      }
      if (specifier.type === "ImportNamespaceSpecifier") {
        return { kind: "namespace", local: specifier.local.name };
      }
      return {
        imported: specifierName(specifier.imported),
        kind: "named",
        local: specifier.local.name,
      };
    }),
  };
}

export function specifierName(node) {
  if (!node) return null;
  if (node.type === "Identifier") return node.name;
  return node.value;
}

export function topLevelDeclarationNames(node) {
  if (node.type === "FunctionDeclaration" || node.type === "ClassDeclaration") {
    return node.id ? [node.id.name] : [];
  }
  if (node.type === "VariableDeclaration") {
    return node.declarations.flatMap((declaration) => bindingNames(declaration.id));
  }
  return [];
}

// Names referenced (in expression position) inside `node` from positions
// that are NOT inside a nested function/method body. Equivalent semantics to
// a babel-traverse pass with a ReferencedIdentifier visitor that filters out
// names with a binding in scope; the typical caller passes a single
// expression whose only "binding in scope" possibilities live inside nested
// functions (which we skip anyway). Hand-rolled recursion is ~10x cheaper
// than wrapping in a synthetic File and running traverse with scope.
export function referencedUndeclaredNames(node) {
  if (!node) return [];
  const names = new Set();
  collectReferencedIdentifierNames(node, names);
  return [...names];
}

export function referencedUndeclaredNamesInVariableDeclarator(node) {
  if (!node) return [];
  const names = new Set();
  if (node.type === "VariableDeclarator") {
    collectReferencedIdentifierNamesInBindingPattern(node.id, names);
    collectReferencedIdentifierNames(node.init, names);
    return [...names];
  }
  collectReferencedIdentifierNames(node, names);
  return [...names];
}

const FUNCTION_TYPES = new Set([
  "FunctionExpression",
  "FunctionDeclaration",
  "ArrowFunctionExpression",
  "ObjectMethod",
  "ClassMethod",
  "ClassPrivateMethod",
]);

const NON_AST_KEYS = new Set([
  "type",
  "start",
  "end",
  "loc",
  "extra",
  "leadingComments",
  "trailingComments",
  "innerComments",
  "comments",
  "tokens",
  "errors",
]);

function collectReferencedIdentifierNames(node, names) {
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const child of node) collectReferencedIdentifierNames(child, names);
    return;
  }
  if (FUNCTION_TYPES.has(node.type)) return;
  switch (node.type) {
    case "Identifier":
      names.add(node.name);
      return;
    case "MemberExpression":
    case "OptionalMemberExpression":
      collectReferencedIdentifierNames(node.object, names);
      if (node.computed) collectReferencedIdentifierNames(node.property, names);
      return;
    case "ObjectProperty":
      if (node.computed) collectReferencedIdentifierNames(node.key, names);
      collectReferencedIdentifierNames(node.value, names);
      return;
    case "VariableDeclarator":
      // node.id is the declaration target (not a reference)
      collectReferencedIdentifierNames(node.init, names);
      return;
    case "ImportSpecifier":
    case "ImportDefaultSpecifier":
    case "ImportNamespaceSpecifier":
    case "ExportSpecifier":
      return;
    default: {
      for (const key of Object.keys(node)) {
        if (NON_AST_KEYS.has(key)) continue;
        collectReferencedIdentifierNames(node[key], names);
      }
    }
  }
}

function collectReferencedIdentifierNamesInBindingPattern(node, names) {
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const child of node) collectReferencedIdentifierNamesInBindingPattern(child, names);
    return;
  }
  switch (node.type) {
    case "Identifier":
      return;
    case "RestElement":
      collectReferencedIdentifierNamesInBindingPattern(node.argument, names);
      return;
    case "AssignmentPattern":
      collectReferencedIdentifierNamesInBindingPattern(node.left, names);
      collectReferencedIdentifierNames(node.right, names);
      return;
    case "ArrayPattern":
      for (const element of node.elements) {
        collectReferencedIdentifierNamesInBindingPattern(element, names);
      }
      return;
    case "ObjectPattern":
      for (const property of node.properties) {
        if (property.type === "RestElement") {
          collectReferencedIdentifierNamesInBindingPattern(property.argument, names);
          continue;
        }
        if (property.computed) {
          collectReferencedIdentifierNames(property.key, names);
        }
        collectReferencedIdentifierNamesInBindingPattern(property.value, names);
      }
      return;
    case "ParenthesizedExpression":
    case "TSAsExpression":
    case "TSSatisfiesExpression":
    case "TSNonNullExpression":
    case "TypeCastExpression":
      collectReferencedIdentifierNamesInBindingPattern(node.expression, names);
      return;
    default:
      collectReferencedIdentifierNames(node, names);
  }
}

export function bindingNames(node) {
  if (!node) return [];
  if (node.type === "Identifier") return [node.name];
  if (node.type === "RestElement") return bindingNames(node.argument);
  if (node.type === "AssignmentPattern") return bindingNames(node.left);
  if (node.type === "ArrayPattern") {
    return node.elements.flatMap((element) => bindingNames(element));
  }
  if (node.type === "ObjectPattern") {
    return node.properties.flatMap((property) => {
      if (property.type === "RestElement") return bindingNames(property.argument);
      return bindingNames(property.value);
    });
  }
  return [];
}
