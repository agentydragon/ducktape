import assert from "node:assert/strict";
import path from "node:path";
import { smoke } from "./smoke.mjs";

for (const source of process.argv.slice(2)) {
  const library = path.basename(source, ".mjs");
  const result = await smoke(library, source);
  assert(result.ok, JSON.stringify(result));
}
