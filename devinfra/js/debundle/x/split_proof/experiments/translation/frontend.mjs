// Front end of the translation checker. Parses JavaScript, resolves what every identifier means, and writes the
// annotated units that `lean/Experiments/Translate.lean` compares.
//
//   node frontend.mjs bundle <bundle.js> --out bundle.json
//   node frontend.mjs tree <outRoot> [--entry entry.js] --out output.json
//
// What the Lean checker trusts from here: acorn (the parse), eslint-scope (which variable an identifier names) and the
// flattening in `convert` below. Everything else is checked in Lean.
//
// The bundle's own export list is kept: it is the chunk's public surface.
// A unit is one top-level statement, or one declarator of a variable list. Identifiers in variable positions become
//   {"type":"Identifier","name":…,"k":K}   with K = L (local, plus binder id "b"), T (module scope), V (an import from
//   outside the tree), G (a global); "d":1 marks the declaration of a module-scope binding.
// Every other identifier (property names, labels) becomes {"type":"Name","name":…}. Literals keep one canonical
// string, positions are dropped, and shorthand properties are written out.
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { literalKey, parseModule, resolveIdentifiers } from "./scope_metadata.mjs";

const DROP = new Set(["start", "end", "range", "loc", "raw", "sourceType"]);

function parse(code, file) {
  try {
    return parseModule(code, acorn.parse, eslintScope, { ecmaVersion: 2024, ignoreEval: true });
  } catch (error) {
    throw new Error(`${file}: ${error.message}`);
  }
}

function convert(node, meaning, binders) {
  if (Array.isArray(node)) return node.map((child) => convert(child, meaning, binders));
  if (node === null || typeof node !== "object") return node;
  switch (node.type) {
    case "Identifier": {
      const m = meaning.get(node);
      if (!m) return { type: "Name", name: node.name };
      const out = { type: "Identifier", name: node.name, k: m.cls };
      if (m.cls === "L") {
        if (!binders.has(m.variable)) binders.set(m.variable, binders.size);
        out.b = binders.get(m.variable);
      }
      if (m.declares) out.d = 1;
      return out;
    }
    case "Literal":
      return { type: "Literal", lit: literalKey(node) };
    case "TemplateElement":
      return { type: "TemplateElement", cooked: node.value.cooked, raw: node.value.raw, tail: node.tail };
  }
  const out = {};
  for (const [key, value] of Object.entries(node)) if (!DROP.has(key)) out[key] = convert(value, meaning, binders);
  if (node.type === "Property") out.shorthand = false;
  return out;
}

/** Units of a module body, plus its import and export declarations. */
function split(parsed, meaning, file, resolveSource) {
  const units = [];
  const imports = [];
  const exports = [];
  for (const { node: statement } of parsed.units) {
    let node = statement;
    if (statement.type === "ImportDeclaration") {
      imports.push({
        from: resolveSource(String(statement.source.value)),
        specs: statement.specifiers.map((s) => ({
          local: s.local.name,
          imported:
            s.type === "ImportDefaultSpecifier"
              ? "default"
              : s.type === "ImportNamespaceSpecifier"
                ? "*"
                : (s.imported.name ?? s.imported.value),
        })),
      });
      continue;
    }
    if (
      statement.type === "ExportAllDeclaration" ||
      statement.type === "ExportDefaultDeclaration" ||
      (statement.type === "ExportNamedDeclaration" && statement.source)
    )
      throw new Error(`${file}: unsupported ${statement.type}${statement.source ? " with a source" : ""}`);
    if (statement.type === "ExportNamedDeclaration") {
      if (!statement.declaration) {
        for (const s of statement.specifiers)
          exports.push({ exported: s.exported.name ?? s.exported.value, local: s.local.name });
        continue;
      }
      node = statement.declaration;
      for (const name of declaredNames(node)) exports.push({ exported: name, local: name });
    }
    units.push(convert(node, meaning, new Map()));
  }
  return { units, imports, exports };
}

function declaredNames(node) {
  const out = [];
  const visit = (pattern) => {
    switch (pattern?.type) {
      case "Identifier":
        out.push(pattern.name);
        break;
      case "ObjectPattern":
        pattern.properties.forEach((p) => visit(p.type === "RestElement" ? p.argument : p.value));
        break;
      case "ArrayPattern":
        pattern.elements.forEach((e) => e && visit(e));
        break;
      case "AssignmentPattern":
        visit(pattern.left);
        break;
      case "RestElement":
        visit(pattern.argument);
        break;
    }
  };
  if (node.type === "VariableDeclaration") node.declarations.forEach((d) => visit(d.id));
  else if (node.id) out.push(node.id.name);
  return out;
}

function listJs(root) {
  const found = [];
  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith(".js") || entry.name.endsWith(".mjs"))
        found.push(path.relative(root, full).split(path.sep).join("/"));
    }
  };
  walk(root);
  return found.sort();
}

/** A bundle: its imports are all external. */
export function frontBundle(file) {
  return bundleFromParsed(parse(readFileSync(file, "utf8"), file), file);
}

export function bundleFromParsed(parsed, file) {
  const meaning = resolveIdentifiers(parsed, () => true);
  const { units, imports, exports } = split(parsed, meaning, file, (spec) => spec);
  return {
    imports: imports.flatMap((d) => d.specs.map((s) => ({ from: d.from, local: s.local, imported: s.imported }))),
    exports,
    units,
  };
}

/** An output tree: an import that does not resolve to a file of the tree is external. */
export function frontTree(root, entry) {
  const files = listJs(root);
  const known = new Set(files);
  const modules = files.map((file) => {
    const resolveSource = (spec) => {
      if (!spec.startsWith(".")) return `ext:${spec}`;
      const target = path.posix.normalize(path.posix.join(path.posix.dirname(file), spec));
      return known.has(target) ? target : `ext:${target}`;
    };
    const parsed = parse(readFileSync(path.join(root, file), "utf8"), file);
    const meaning = resolveIdentifiers(parsed, (spec) => resolveSource(spec).startsWith("ext:"));
    return { path: file, ...split(parsed, meaning, file, resolveSource) };
  });
  if (!known.has(entry)) throw new Error(`entry ${entry} is not in ${root}`);
  return { entry, modules };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [mode, input, ...rest] = process.argv.slice(2);
  const option = (name, fallback) => (rest.includes(`--${name}`) ? rest[rest.indexOf(`--${name}`) + 1] : fallback);
  const out = option("out");
  if (!out || !["bundle", "tree"].includes(mode))
    throw new Error("usage: frontend.mjs bundle <file> --out <json> | tree <dir> [--entry <file>] --out <json>");
  const result = mode === "bundle" ? frontBundle(input) : frontTree(input, option("entry", "entry.js"));
  writeFileSync(out, JSON.stringify(result));
  const count = mode === "bundle" ? result.units.length : result.modules.reduce((n, m) => n + m.units.length, 0);
  console.log(`${mode}: ${count} units${mode === "tree" ? ` in ${result.modules.length} modules` : ""} -> ${out}`);
}
