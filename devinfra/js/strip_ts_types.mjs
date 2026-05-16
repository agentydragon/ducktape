#!/usr/bin/env node
// Strip TypeScript syntax from a single file using `ts.transpileModule`.
// No type checking, no module resolution — just an AST transform.
//
// Two source rewrites first patch over Zod 3-isms in `openapi-zod-client`
// output so the result works under Zod 4:
//
//   1. Drop `.optional()` immediately after `z.literal("X")`. The generator
//      emits `z.literal("X").optional().default("X")` for Pydantic `Literal`
//      fields with a default; Zod 4's `z.discriminatedUnion` extracts a
//      discriminator value per option at schema-build time and `.optional()`
//      pulls `undefined` into the extracted set, so multiple options collide
//      with `Duplicate discriminator value "undefined"`. Stripping
//      `.optional()` leaves `z.literal("X").default("X")` — input may be the
//      literal or omitted; output is always the literal; clean discriminator.
//
//   2. Inject a `z.string()` key into single-arg `z.record(V)` calls. Zod 4
//      requires `z.record(key, value)`; with only `V` it reads `V` as the
//      key schema and rejects every value at parse time. OpenAPI dict
//      properties always have string keys, so this is safe.

import { readFileSync, writeFileSync } from "node:fs";
import ts from "typescript";

const [, , input, output] = process.argv;
if (!input || !output) {
  console.error("usage: strip_ts_types.mjs <input.ts> <output.mjs>");
  process.exit(1);
}

const sourceText = readFileSync(input, "utf8").replaceAll(/(\.literal\("[^"]+"\))\s*\.optional\(\)/g, "$1");

function isZRecord(expression) {
  return (
    ts.isPropertyAccessExpression(expression) &&
    ts.isIdentifier(expression.expression) &&
    expression.expression.text === "z" &&
    expression.name.text === "record"
  );
}

function injectStringKey(context) {
  return (rootNode) => {
    const visit = (node) => {
      if (ts.isCallExpression(node) && isZRecord(node.expression) && node.arguments.length === 1) {
        return ts.factory.updateCallExpression(node, node.expression, node.typeArguments, [
          ts.factory.createCallExpression(
            ts.factory.createPropertyAccessExpression(
              ts.factory.createIdentifier("z"),
              ts.factory.createIdentifier("string")
            ),
            undefined,
            []
          ),
          ts.visitNode(node.arguments[0], visit),
        ]);
      }
      return ts.visitEachChild(node, visit, context);
    };
    return ts.visitNode(rootNode, visit);
  };
}

const { outputText } = ts.transpileModule(sourceText, {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    verbatimModuleSyntax: true,
  },
  transformers: { before: [injectStringKey] },
  fileName: input,
});
writeFileSync(output, outputText);
