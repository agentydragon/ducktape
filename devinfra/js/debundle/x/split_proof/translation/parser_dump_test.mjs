import { strict as assert } from "node:assert";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { acorn, dump, eslintScope } from "./parser.mjs";
import { dumpParsed } from "./annotated_estree.mjs";
import { parseModule } from "./scope_metadata.mjs";

const source = readFileSync(new URL("./scope_metadata.mjs", import.meta.url), "utf8");
const serialized = JSON.stringify(dump(source));
assert.equal(
  createHash("sha256").update(serialized).digest("hex"),
  "9fbbe8ec8a5fb79575d1183191522e40403e4c0d0a4fa7dc011fbe0c14065d1e"
);

for (const options of [
  undefined,
  { includeOpaqueCaptures: true },
  { legacy: true },
  { includeOpaqueCaptures: true, legacy: true },
]) {
  const expected = dumpParsed(parseModule(source, acorn.parse, eslintScope), options);
  assert.equal(JSON.stringify(dump(source, options)), JSON.stringify(expected));
}
