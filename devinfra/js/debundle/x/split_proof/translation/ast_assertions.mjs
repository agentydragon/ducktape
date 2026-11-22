import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import { topLevelStatements } from "./scope_metadata.mjs";

const IGNORED_AST_FIELDS = new Set(["start", "end", "range", "loc", "sourceText"]);

export function astShape(node) {
  if (Array.isArray(node)) return node.map(astShape);
  if (node === null || typeof node !== "object") return typeof node === "bigint" ? { bigintValue: String(node) } : node;
  return Object.fromEntries(
    Object.entries(node)
      .filter(([key]) => !IGNORED_AST_FIELDS.has(key) && !(node.type === "Literal" && key === "raw"))
      .map(([key, value]) => [key, astShape(value)])
  );
}

export function isGeneratedScaffolding(node) {
  return (
    node.type === "ImportDeclaration" ||
    (node.type === "ExportNamedDeclaration" && node.declaration === null && node.source === null)
  );
}

export function parseSourceUnits(source) {
  const program = acorn.parse(source, { sourceType: "module", ecmaVersion: "latest" });
  return topLevelStatements(program, source).map(({ node }) => node);
}

export function assertSelectedSourceUnits(sourceUnits, outputText, selected, context) {
  const emitted = topLevelStatements(
    acorn.parse(outputText, { sourceType: "module", ecmaVersion: "latest" }),
    outputText
  )
    .map(({ node }) => node)
    .filter((node) => !isGeneratedScaffolding(node))
    .map(astShape);
  const expected = selected
    .map((selector) => {
      const index = typeof selector === "number" ? selector : Number(selector.slice(1));
      assert(Number.isInteger(index) && index >= 0 && sourceUnits[index], `${context}: invalid unit ${selector}`);
      return sourceUnits[index];
    })
    .filter((node) => !isGeneratedScaffolding(node))
    .map(astShape);
  assert.deepEqual(emitted, expected, `${context}: output must reparse to the selected source ESTree units`);
}

export function assertSourceUnitsReparse(source, outputDir, outputFiles, context = "output") {
  const expected = parseSourceUnits(source)
    .filter((node) => !isGeneratedScaffolding(node))
    .map(astShape)
    .sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)));
  const actual = outputFiles
    .flatMap((file) => {
      const text = readFileSync(path.join(outputDir, file), "utf8");
      return topLevelStatements(acorn.parse(text, { sourceType: "module", ecmaVersion: "latest" }), text)
        .map(({ node }) => node)
        .filter((node) => !isGeneratedScaffolding(node))
        .map(astShape);
    })
    .sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)));
  assert.deepEqual(
    actual,
    expected,
    `${context}: reparsed output must contain every original non-scaffolding ESTree unit`
  );
}
