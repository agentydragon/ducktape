import { createClient } from "@hey-api/openapi-ts";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { withRemoteReferencesDisabled } from "./schema_remote_fetch_guard.mjs";

const [inputArgument, outputArgument, configArgument] = process.argv.slice(2);
if (!inputArgument || !outputArgument || !configArgument) {
  throw new Error("usage: openapi-zod-codegen <input.json> <output-dir> <config.mjs>");
}

const configPath = resolve(configArgument);

await withRemoteReferencesDisabled(async () => {
  const configModule = await import(pathToFileURL(configPath).href);
  if (!configModule.default || typeof configModule.default !== "object" || Array.isArray(configModule.default)) {
    throw new Error("OpenAPI Zod config must default-export one configuration object");
  }

  await createClient({
    configFile: configPath,
    input: resolve(inputArgument),
    output: resolve(outputArgument),
  });
});
