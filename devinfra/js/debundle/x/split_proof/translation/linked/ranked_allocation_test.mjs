import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { split } from "../testing/split.mjs";
import { moduleEvaluationOrder, prepare, root } from "./harness.mjs";

const spec = {
  entry: "entry.mjs",
  modules: { "part.mjs": ["#0"], "entry.mjs": ["#1"] },
  order: ["part.mjs"],
  sequential: true,
};
const require = "all-outcomes";
const dir = path.join(root, "array-read");
const { sourceFile, specFile } = prepare(dir, { source: "const x=1; export const y=[x];", provider: "", spec });
const request = { source: sourceFile, spec: specFile, require, out: path.join(dir, "out") };
const built = split(request);
assert.equal(built.status, "certified", built.log);
assert.equal(built.guarantee, require, built.log);
assert.deepEqual(built.files, ["entry.mjs", "part.mjs"]);
assert.deepEqual(moduleEvaluationOrder(request.out, spec.entry), ["part.mjs", "entry.mjs"]);
assert.equal(split({ ...request, checkOutput: true }).status, "certified");

const entry = path.join(request.out, spec.entry);
const part = path.join(request.out, "part.mjs");
const emitted = readFileSync(entry, "utf8");
assert.match(readFileSync(part, "utf8"), /const x = 1;/);
assert.match(emitted, /const y = \[x\];/);
const original = await import(pathToFileURL(sourceFile));
const output = await import(pathToFileURL(entry));
assert.deepEqual(Object.keys(output), ["y"]);
assert.deepEqual(output.y, original.y);
assert.deepEqual(output.y, [(await import(pathToFileURL(part))).x]);
assert.notEqual(output.y, original.y);
assert.equal((await import(pathToFileURL(entry))).y, output.y);

// Equal values are insufficient: the observed initializer must still use x.
writeFileSync(entry, emitted.replace("[x]", "[1]"));
const alternate = await import(`${pathToFileURL(entry)}?mutated`);
assert.deepEqual(alternate.y, output.y);
assert.notEqual(alternate.y, output.y);
assert.equal(split({ ...request, checkOutput: true }).status, "refused");
