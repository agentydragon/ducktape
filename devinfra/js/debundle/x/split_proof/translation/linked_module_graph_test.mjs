import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parse } from "acorn";
import { lower, moduleEvaluationOrder, observe, prepare, root } from "./testing/linked_programs.mjs";

// Sixteen provider/reader pairs reach the staged linking threshold of 32 executable units.
const count = process.argv.includes("--full-graph") ? 48 : 16;
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

const aliasDir = path.join(root, "public-alias");
const aliasFiles = prepare(aliasDir, {
  source: `
    let value = 17;
    function set() { value = 42; }
    const offset = 3;
    const read = () => value + offset;
    export { value as publicValue, offset as "public-offset", set, read };
  `,
  provider: "",
  spec: { entry, modules: { [entry]: ["#0", "#1", "#2", "#4"], "reader.mjs": ["#3"] }, order: ["reader.mjs"] },
});
const aliasOut = path.join(aliasDir, "out");
const alias = lower(aliasFiles.sourceFile, aliasFiles.specFile, aliasOut);
assert.equal(alias.verdict.status, "certified", alias.log);
const reader = parse(readFileSync(path.join(aliasOut, "reader.mjs"), "utf8"), {
  sourceType: "module",
  ecmaVersion: "latest",
});
const request = reader.body.find((node) => node.type === "ImportDeclaration");
assert.equal(request?.source.value, "./entry.mjs");
assert.deepEqual(
  Object.fromEntries(request.specifiers.map(({ imported, local }) => [local.name, imported.name ?? imported.value])),
  { value: "publicValue", offset: "public-offset" }
);
for (const file of [aliasFiles.sourceFile, path.join(aliasOut, entry)]) {
  const namespace = await import(pathToFileURL(file).href);
  assert.deepEqual(Object.keys(namespace), ["public-offset", "publicValue", "read", "set"]);
  assert.equal(namespace.read(), 20);
  assert.equal(namespace.publicValue, 17);
  namespace.set();
  assert.equal(namespace.read(), 45);
  assert.equal(namespace.publicValue, 42);
}
