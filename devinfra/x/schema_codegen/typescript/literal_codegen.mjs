import { readFileSync, writeFileSync } from "node:fs";

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) throw new Error("usage: literal_codegen.mjs schema.json output.ts");

const schema = JSON.parse(readFileSync(inputPath, "utf8"));
if (schema.type !== "string" || !Array.isArray(schema.enum) || schema.enum.length === 0) {
  throw new Error(`${inputPath} must be a non-empty string enum JSON Schema`);
}
if (!schema.enum.every((value) => typeof value === "string")) {
  throw new Error(`${inputPath} contains a non-string enum value`);
}

const values = JSON.stringify(schema.enum, null, 2);
const typeName = schema.title;
if (typeof typeName !== "string" || !/^[A-Z][A-Za-z0-9]*$/.test(typeName)) {
  throw new Error(`${inputPath} must have a TypeScript-compatible PascalCase title`);
}
const constName = typeName[0].toLowerCase() + typeName.slice(1) + "Values";
writeFileSync(
  outputPath,
  `export const ${constName} = ${values} as const;\nexport type ${typeName} = (typeof ${constName})[number];\n`
);
