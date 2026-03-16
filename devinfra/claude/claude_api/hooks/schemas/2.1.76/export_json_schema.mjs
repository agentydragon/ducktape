/**
 * Export Zod hook schemas as JSON Schema files.
 *
 * Usage: node export_json_schema.mjs <name>=<file> [<name>=<file> ...]
 *
 * Example: node export_json_schema.mjs hookOutput=hook_output.json AnyHookInput=hook_input.json
 */

import { writeFileSync } from "node:fs";
import { z } from "zod";
import { hookOutput, AnyHookInput } from "./hooks.zod.js";

const schemas = { hookOutput, AnyHookInput };
const args = process.argv.slice(2);

if (args.length === 0) {
  console.error(
    `Usage: export_json_schema.mjs <name>=<file> [...]\nAvailable: ${Object.keys(schemas).join(", ")}`,
  );
  process.exit(1);
}

for (const arg of args) {
  const [name, outFile] = arg.split("=", 2);
  if (!schemas[name]) {
    console.error(`Unknown schema: ${name}. Available: ${Object.keys(schemas).join(", ")}`);
    process.exit(1);
  }
  const jsonSchema = z.toJSONSchema(schemas[name], { target: "draft-2020-12" });
  const content = JSON.stringify(jsonSchema, null, 2) + "\n";
  if (outFile) {
    writeFileSync(outFile, content);
  } else {
    process.stdout.write(content);
  }
}
