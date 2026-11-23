import { createClient } from "@hey-api/openapi-ts";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { withRemoteReferencesDisabled } from "./schema_remote_fetch_guard.mjs";

const [inputArgument, outputArgument] = process.argv.slice(2);
if (!inputArgument || !outputArgument) {
  throw new Error("usage: openapi-zod-codegen <input.json> <output-dir>");
}

await withRemoteReferencesDisabled(async () => {
  await createClient({
    configFile: fileURLToPath(new URL("./openapi-zod.config.mjs", import.meta.url)),
    input: resolve(inputArgument),
    output: resolve(outputArgument),
  });
});
