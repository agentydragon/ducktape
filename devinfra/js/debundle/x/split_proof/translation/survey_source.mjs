// Inputs shared by the host-feature inventory and the census of reflective JavaScript.
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";

/** Names that are the global object itself: `window.fetch` is the global `fetch`. */
export const GLOBAL_OBJECTS = new Set(["globalThis", "window", "self", "global"]);

const SOURCE_EXTENSIONS = new Set([".js", ".mjs", ".cjs"]);

export function sourceFiles(root) {
  if (root.startsWith("@"))
    return readFileSync(root.slice(1), "utf8")
      .split("\n")
      .filter((line) => line !== "");
  if (statSync(root).isFile()) return [root];
  return readdirSync(root, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile() && SOURCE_EXTENSIONS.has(path.extname(entry.name)))
    .map((entry) => path.join(entry.parentPath, entry.name))
    .sort();
}

/** Prefer modules, then accept classic scripts and CommonJS top-level returns. */
export function parseSurveySource(source) {
  const options = { ecmaVersion: "latest", allowHashBang: true, ranges: true };
  let ast;
  try {
    ast = acorn.parse(source, { ...options, sourceType: "module" });
  } catch {
    ast = acorn.parse(source, { ...options, sourceType: "script", allowReturnOutsideFunction: true });
  }
  const scopes = eslintScope.analyze(ast, { ecmaVersion: 2024, sourceType: ast.sourceType, ignoreEval: true });
  return { ast, references: scopes.globalScope.through };
}

export const staticMemberKey = (member) =>
  !member.computed && member.property.type === "Identifier"
    ? member.property.name
    : member.computed && member.property.type === "Literal" && typeof member.property.value === "string"
      ? member.property.value
      : undefined;
