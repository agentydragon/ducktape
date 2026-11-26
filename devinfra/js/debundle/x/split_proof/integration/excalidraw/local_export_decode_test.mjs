import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dump } from "../../translation/parser.mjs";
import { moduleGraphFixture } from "../../translation/linked/module_graph_fixture.mjs";

// Use the production parser on the same complete source as the #32 CLI fixture.
const sources = [
  moduleGraphFixture().source,
  "const seed = 42; export { seed as exposed }; const result = seed;",
  'const seed = 42; export { seed as publicName, seed as "with-dash" }; export {}; const result = seed;',
  "export const declared = 1;",
  "export default 7;",
  'export { value } from "./provider.mjs";',
  'import { value } from "./provider.mjs"; export { value };',
  "export const declared = 1; export { declared as alias };",
];
const run = spawnSync(process.env.LOCAL_EXPORT_DECODE_TEST, [], {
  encoding: "utf8",
  input: JSON.stringify(sources.map((source) => dump(source))) + "\n",
  maxBuffer: 64 * 1024 * 1024,
});
if (run.error) throw run.error;
assert.equal(run.status, 0, run.stdout + run.stderr);
