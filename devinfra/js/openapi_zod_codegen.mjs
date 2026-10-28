import { createClient } from "@hey-api/openapi-ts";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [inputArgument, outputArgument, configArgument] = process.argv.slice(2);
if (!inputArgument || !outputArgument || !configArgument) {
  throw new Error("usage: openapi-zod-codegen <input.json> <output-dir> <config.mjs>");
}

const configPath = resolve(configArgument);
const configModule = await import(pathToFileURL(configPath).href);
if (!configModule.default || typeof configModule.default !== "object" || Array.isArray(configModule.default)) {
  throw new Error("OpenAPI Zod config must default-export one configuration object");
}

const originalFetch = globalThis.fetch;
globalThis.fetch = async (input) => {
  const url = String(input);
  return new Response(`Remote OpenAPI references are disabled: ${url}`, {
    status: 400,
    statusText: "Remote references disabled",
  });
};

try {
  await createClient({
    configFile: configPath,
    input: resolve(inputArgument),
    output: resolve(outputArgument),
  });
} finally {
  globalThis.fetch = originalFetch;
}
