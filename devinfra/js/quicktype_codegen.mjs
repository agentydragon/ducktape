import { readFile, writeFile } from "node:fs/promises";
import { parseArgs } from "node:util";
import { InputData, JSONSchemaInput, quicktype } from "quicktype-core";
import { withRemoteReferencesDisabled } from "./schema_remote_fetch_guard.mjs";

const {
  positionals: [name, language, inputPath, outputPath],
  values: rendererOptions,
} = parseArgs({ strict: false, allowPositionals: true });
if (!name || !language || !inputPath || !outputPath) {
  throw new Error("usage: quicktype-codegen <name> <language> <schema.json> <output> [--renderer-option=value]");
}

await withRemoteReferencesDisabled(async () => {
  const input = new JSONSchemaInput();
  await input.addSource({ name, schema: await readFile(inputPath, "utf8") });
  const inputData = new InputData();
  inputData.addInput(input);
  const result = await quicktype({ inputData, lang: language, rendererOptions });
  await writeFile(outputPath, `${result.lines.join("\n")}\n`);
});
