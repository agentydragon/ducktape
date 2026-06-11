#!/usr/bin/env node
// Strip TypeScript syntax from a single file using `ts.transpileModule`.
// No type checking, no module resolution — just an AST transform.

import { readFileSync, writeFileSync } from "node:fs";
import ts from "typescript";

const [, , input, output] = process.argv;
if (!input || !output) {
  console.error("usage: strip_ts_types.mjs <input.ts> <output.mjs>");
  process.exit(1);
}

const { outputText } = ts.transpileModule(readFileSync(input, "utf8"), {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    verbatimModuleSyntax: true,
  },
  fileName: input,
});
writeFileSync(output, outputText);
