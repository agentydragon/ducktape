import openapiTS, { astToString } from "openapi-typescript";
import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { withRemoteReferencesDisabled } from "./schema_remote_fetch_guard.mjs";

const [inputArgument, outputArgument] = process.argv.slice(2);
if (!inputArgument || !outputArgument) {
  throw new Error("usage: openapi-typescript-codegen <input.json> <output.d.ts>");
}

await withRemoteReferencesDisabled(async () => {
  const ast = await openapiTS(pathToFileURL(resolve(inputArgument)), {
    rootTypes: true,
    rootTypesNoSchemaPrefix: true,
  });
  await writeFile(resolve(outputArgument), astToString(ast), "utf-8");
});
