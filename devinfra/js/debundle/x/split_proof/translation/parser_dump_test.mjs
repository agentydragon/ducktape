import { strict as assert } from "node:assert";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { acorn, dump, eslintScope } from "./parser.mjs";
import { dumpParsed } from "./annotated_estree.mjs";
import { parseModule } from "./scope_metadata.mjs";

const source = readFileSync(new URL("./scope_metadata.mjs", import.meta.url), "utf8");
// Pin parser output for a fixed source. The implementation itself remains a larger
// differential corpus below and can change without invalidating the reference digest.
const fixture = `import { external as alias } from "./dep.mjs";
export class Holder {
  #value = 0.5;
  read(key = this.#value) { return () => alias(key, this.#value); }
}
const tag = (strings, ...values) => [strings.raw, values];
export const message = tag\`line\\n\${new Holder().read()}\`;`;
const serialized = JSON.stringify(dump(fixture));
assert.equal(
  createHash("sha256").update(serialized).digest("hex"),
  "fb53ad32644d6ff8708d965153b49e9ab1fcfbd7a90081b350048b395e627fb3"
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
