import openapiTS, { astToString } from "openapi-typescript";
import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [inputArgument, outputArgument] = process.argv.slice(2);
if (!inputArgument || !outputArgument) {
  throw new Error("usage: openapi-typescript-codegen <input.json> <output.d.ts>");
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
  const ast = await openapiTS(pathToFileURL(resolve(inputArgument)), {
    rootTypes: true,
    rootTypesNoSchemaPrefix: true,
  });
  await writeFile(resolve(outputArgument), astToString(ast), "utf-8");
} finally {
  globalThis.fetch = originalFetch;
}
