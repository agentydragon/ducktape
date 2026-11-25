import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { assertCertified, lower, prepare, root } from "./harness.mjs";

// Each of the 512 import specifiers resolves a cell in the same provider scope.
const names = Array.from({ length: 512 }, (_, i) => "value" + i);
const provider = names.map((v, i) => "export const " + v + " = " + i + ";").join("\n");
const source =
  "import {" +
  names.join(",") +
  '} from "./provider.mjs";\n' +
  "const helper = () => 1; export const answer = value511 + helper();";
const dir = path.join(root, "wide-provider");
const files = prepare(dir, {
  source,
  provider,
  spec: {
    entry: "entry.mjs",
    modules: { "entry.mjs": ["#0", "#2"], "helper.mjs": ["#1"] },
    order: ["helper.mjs"],
  },
});
const out = path.join(dir, "out");
const options = [];
const { verdict: proof, log } = lower(files.sourceFile, files.specFile, out, options);
assert.equal(proof.strategy, "structural", log);
assertCertified(proof, log);
assert.deepEqual(lower(files.sourceFile, files.specFile, out, [...options, "--check-output"]).verdict, proof);
for (const file of [files.sourceFile, path.join(out, "entry.mjs")])
  assert.equal((await import(pathToFileURL(file))).answer, 512);
