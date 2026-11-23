import assert from "node:assert/strict";
import path from "node:path";
import { lower, moduleEvaluationOrder, observe, prepare, root } from "./testing/linked_programs.mjs";

const count = 48;
const names = Array.from({ length: count }, (_, index) => `reader${index}`);
const expected = Object.fromEntries(names.map((name, index) => [name, `value${index}`]));
const values = names.map((name, index) => `const data${index} = ${JSON.stringify(expected[name])};`);
const readers = names.map((name, index) => `const ${name} = data${index};`);
const source = [...values, ...readers, `export { ${names.join(", ")} };`].join("\n");

const entry = "entry.mjs";
const modules = {};
for (const [group, offset] of [
  ["provider", 0],
  ["consumer", count],
]) {
  for (let index = 0; index < count; index++) {
    const module = `${group}${Math.floor(index / 8)}.mjs`;
    (modules[module] ??= []).push(`#${offset + index}`);
  }
}
const order = Object.keys(modules);
modules[entry] = [`#${2 * count}`];

const dir = path.join(root, "module-graph");
const files = prepare(dir, { source, provider: "", spec: { entry, modules, order, sequential: true } });
const out = path.join(dir, "out");
const { verdict, log } = lower(files.sourceFile, files.specFile, out);
assert.equal(verdict.status, "certified", log);
assert.equal(verdict.guarantee, "normal-completion", log);
assert.deepEqual(lower(files.sourceFile, files.specFile, out, ["--check-output"]).verdict, verdict);
assert.deepEqual(moduleEvaluationOrder(out, entry), [...order, entry]);
for (const file of [files.sourceFile, path.join(out, entry)]) {
  assert.deepEqual(observe(file, names), expected);
}
