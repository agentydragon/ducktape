#!/usr/bin/env node
// Rewrite `{"const": "X"}` to `{"enum": ["X"]}` recursively in an OpenAPI JSON
// document. Used to coerce `openapi-zod-client` into emitting `z.enum(["X"])`
// (a Zod literal) for Pydantic-`Literal` discriminator fields. Without this,
// it emits `z.string().optional().default("X")`, which Zod 4's
// `z.discriminatedUnion` rejects with "Invalid discriminated union option at
// index N" at schema-build time.

import { readFileSync, writeFileSync } from "node:fs";

const [, , input, output] = process.argv;
if (!input || !output) {
  console.error("usage: openapi_const_to_enum.mjs <input.json> <output.json>");
  process.exit(1);
}

function rewrite(node) {
  if (Array.isArray(node)) return node.map(rewrite);
  if (node && typeof node === "object") {
    if (typeof node.const === "string" && node.enum === undefined) {
      const { const: literal, ...rest } = node;
      return rewrite({ ...rest, enum: [literal] });
    }
    return Object.fromEntries(Object.entries(node).map(([k, v]) => [k, rewrite(v)]));
  }
  return node;
}

writeFileSync(output, JSON.stringify(rewrite(JSON.parse(readFileSync(input, "utf8")))));
