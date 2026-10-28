import { compileFromFile } from "json-schema-to-typescript";
import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const [inputArgument, outputArgument] = process.argv.slice(2);
if (!inputArgument || !outputArgument) {
  throw new Error("usage: json-schema-typescript-codegen <input.json> <output.d.ts>");
}

const declarations = await compileFromFile(resolve(inputArgument), {
  bannerComment: "",
  $refOptions: { resolve: { http: false } },
});
await writeFile(resolve(outputArgument), declarations, "utf-8");
