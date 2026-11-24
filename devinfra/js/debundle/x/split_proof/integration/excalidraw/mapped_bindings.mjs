// Inventory candidates in a single development ESM file and its sibling .map.
// The locations belong only to this JS file. No production placements are emitted.
import { readFileSync } from "node:fs";
import { SourceMap } from "node:module";
import { acorn } from "../../translation/parser.mjs";

export function mappedBindings(code, payload) {
  const map = new SourceMap(payload);
  const program = acorn.parse(code, { ecmaVersion: "latest", sourceType: "module", locations: true });
  const bindings = [];
  for (const stmt of program.body) {
    const decl =
      stmt.type === "ExportNamedDeclaration" || stmt.type === "ExportDefaultDeclaration" ? stmt.declaration : stmt;
    const ids =
      decl?.type === "VariableDeclaration"
        ? decl.declarations.map((v) => v.id)
        : ["ClassDeclaration", "FunctionDeclaration"].includes(decl?.type)
          ? [decl.id]
          : [];
    for (const id of ids) {
      if (id?.type !== "Identifier") continue;
      const { line, column } = id.loc.start;
      const entry = map.findEntry(line - 1, column);
      // A greatest-lower-bound lookup can land on the previous binding.
      // Keep only mappings starting at this exact identifier.
      if (entry.generatedLine !== line - 1 || entry.generatedColumn !== column || !entry.originalSource) continue;
      bindings.push({
        binding: id.name,
        source: entry.originalSource,
        line: entry.originalLine + 1,
        column: entry.originalColumn + 1,
      });
    }
  }
  return bindings;
}

if (import.meta.main) {
  const [file] = process.argv.slice(2);
  if (!file) throw new Error("usage: mapped_bindings <absolute path to development .js>");
  for (const binding of mappedBindings(readFileSync(file, "utf8"), JSON.parse(readFileSync(file + ".map", "utf8")))) {
    console.log(JSON.stringify(binding));
  }
}
