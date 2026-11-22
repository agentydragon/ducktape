// Parse the bundle and candidate modules as ESTree Programs and attach scope metadata.
// Lean derives the units and import/export tables used by the translation checker.
//
//   node frontend.mjs bundle <bundle.js> --out bundle.json
//   node frontend.mjs tree <outRoot> [--entry entry.js] --out output.json
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { annotateProgram, parseModule, resolveIdentifiers, topLevelStatements } from "./scope_metadata.mjs";

function parse(code, file) {
  try {
    return parseModule(code, acorn.parse, eslintScope, { ecmaVersion: 2024, ignoreEval: true });
  } catch (error) {
    throw new Error(`${file}: ${error.message}`);
  }
}

/** Independently enumerate the body units for the untrusted pairing and diagnostic tools. */
export function bodyUnits(program) {
  return topLevelStatements(program).flatMap(({ node }) => {
    switch (node.type) {
      case "ImportDeclaration":
      case "ExportAllDeclaration":
      case "ExportDefaultDeclaration":
        return [];
      case "ExportNamedDeclaration":
        return !node.source && node.declaration ? [node.declaration] : [];
      default:
        return [node];
    }
  });
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

export function resolveSource(known, file, spec) {
  if (!spec.startsWith(".")) return `ext:${spec}`;
  const target = path.posix.normalize(path.posix.join(path.posix.dirname(file), spec));
  return known.has(target) ? target : `ext:${target}`;
}

/** A bundle's imports are all external. */
export function frontBundle(file) {
  return bundleFromParsed(parse(readFileSync(file, "utf8"), file));
}

export function bundleFromParsed(parsed) {
  return {
    program: annotateProgram(
      parsed,
      resolveIdentifiers(parsed, () => true)
    ),
  };
}

/** A candidate import that does not resolve to a file of the tree is external. */
export function frontTree(root, entry) {
  const files = listJs(root);
  const known = new Set(files);
  const modules = files.map((file) => {
    const parsed = parse(readFileSync(path.join(root, file), "utf8"), file);
    const meaning = resolveIdentifiers(parsed, (spec) => resolveSource(known, file, spec).startsWith("ext:"));
    return { path: file, program: annotateProgram(parsed, meaning) };
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
  console.log(`${mode}${mode === "tree" ? `: ${result.modules.length} modules` : ""} -> ${out}`);
}
