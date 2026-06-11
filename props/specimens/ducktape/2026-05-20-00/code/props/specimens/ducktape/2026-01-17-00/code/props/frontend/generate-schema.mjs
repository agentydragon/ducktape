#!/usr/bin/env node
/**
 * Wrapper script to run openapi-typescript.
 * Usage: node generate-schema.mjs <input.json> <output.d.ts>
 *
 * Paths are used as-is (already resolved by Bazel).
 */
import openapiTS, { astToString } from 'openapi-typescript';
import fs from 'fs/promises';
import { dirname } from 'path';

const args = process.argv.slice(2);

if (args.length < 2) {
  console.error('Usage: generate-schema.mjs <input.json> <output.d.ts>');
  process.exit(1);
}

const [inputFile, outputFile] = args;

async function main() {
  // Read the OpenAPI schema
  const schemaJson = JSON.parse(await fs.readFile(inputFile, 'utf-8'));

  // Generate TypeScript types - returns an AST, use astToString to convert
  const ast = await openapiTS(schemaJson);
  const output = astToString(ast);

  // Write output
  await fs.mkdir(dirname(outputFile), { recursive: true });
  await fs.writeFile(outputFile, output);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
