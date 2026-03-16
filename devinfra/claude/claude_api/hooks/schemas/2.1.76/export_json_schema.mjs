/**
 * Export a Zod hook schema as JSON Schema to stdout.
 *
 * Usage: node export_json_schema.mjs <hookOutput|AnyHookInput>
 */

import { z } from "zod";
import { hookOutput, AnyHookInput } from "./hooks.zod.js";

const schemas = { hookOutput, AnyHookInput };
const name = process.argv[2];

if (!name || !schemas[name]) {
  console.error(`Usage: export_json_schema.mjs <${Object.keys(schemas).join("|")}>`);
  process.exit(1);
}

const jsonSchema = z.toJSONSchema(schemas[name], { target: "draft-2020-12" });
process.stdout.write(JSON.stringify(jsonSchema, null, 2) + "\n");
