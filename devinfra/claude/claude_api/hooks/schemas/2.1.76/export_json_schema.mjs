/**
 * Export a Zod hook schema as JSON Schema.
 *
 * Usage: node export_json_schema.mjs <hookOutput|AnyHookInput> [output_file]
 *
 * Writes to output_file if provided, otherwise stdout.
 */

import { writeFileSync } from "node:fs";
import { z } from "zod";
import { hookOutput, AnyHookInput } from "./hooks.zod.js";

const schemas = { hookOutput, AnyHookInput };
const name = process.argv[2];
const outFile = process.argv[3];

if (!name || !schemas[name]) {
  console.error(
    `Usage: export_json_schema.mjs <${Object.keys(schemas).join("|")}> [output_file]`,
  );
  process.exit(1);
}

const jsonSchema = z.toJSONSchema(schemas[name], { target: "draft-2020-12" });
const content = JSON.stringify(jsonSchema, null, 2) + "\n";

if (outFile) {
  writeFileSync(outFile, content);
} else {
  process.stdout.write(content);
}
