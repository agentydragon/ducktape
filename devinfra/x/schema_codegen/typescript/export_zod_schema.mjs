import { writeFileSync } from "node:fs";
import { z } from "zod";

import { taskSchema } from "./zod_model.js";

const [outputPath] = process.argv.slice(2);
if (!outputPath) throw new Error("usage: export_zod_schema.mjs output.json");
writeFileSync(outputPath, JSON.stringify(z.toJSONSchema(taskSchema, { target: "draft-2020-12" }), null, 2) + "\n");
