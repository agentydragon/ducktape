import assert from "node:assert/strict";
import { cpSync, readFileSync, writeFileSync } from "node:fs";
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
function build(name, source) {
  const dir = path.join(root, name);
  const { sourceFile, specFile } = prepare(dir, { source, provider: "", spec });
  const request = { source: sourceFile, spec: specFile, require, out: path.join(dir, "out") };
  const built = split(request);
  assert.equal(built.status, "certified", built.log);
  assert.equal(built.guarantee, require, built.log);
  assert.deepEqual(built.files, ["entry.mjs", "part.mjs"]);
  assert.deepEqual(moduleEvaluationOrder(request.out, spec.entry), ["part.mjs", "entry.mjs"]);
  assert.equal(split({ ...request, checkOutput: true }).status, "certified");
  return { request, entry: path.join(request.out, spec.entry), part: path.join(request.out, "part.mjs") };
}

const { request, entry, part } = build("array-read", "const x=1; export const y=[x];");
const emitted = readFileSync(entry, "utf8");
assert.match(readFileSync(part, "utf8"), /const x = 1;/);
assert.match(emitted, /const y = \[x\];/);
const original = await import(pathToFileURL(request.source));
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

async function assertAlias(name, source, literal, shape) {
  const { request: aliasRequest, entry: aliasEntry, part: aliasPart } = build(name, source);
  const provider = await import(pathToFileURL(aliasPart));
  const namespace = await import(pathToFileURL(aliasEntry));
  const raw = await import(pathToFileURL(aliasRequest.source));
  assert.match(readFileSync(aliasPart, "utf8"), /const x = /);
  assert.deepEqual(Object.keys(namespace), ["y"]);
  assert.deepEqual(namespace.y, shape);
  assert.deepEqual(raw.y, namespace.y);
  assert.equal(namespace.y, provider.x);
  assert.notEqual(namespace.y, raw.y);

  // A second module graph allocates a different object, while its entry
  // still aliases its own provider. Mutations are visible through each alias.
  const fresh = aliasRequest.out + "-fresh";
  cpSync(aliasRequest.out, fresh, { recursive: true });
  const second = await import(pathToFileURL(path.join(fresh, spec.entry)));
  assert.equal(second.y, (await import(pathToFileURL(path.join(fresh, "part.mjs")))).x);
  assert.notEqual(second.y, namespace.y);
  provider.x.observed = 7;
  assert.equal(namespace.y.observed, 7);
  assert.equal(second.y.observed, undefined);
  delete provider.x.observed;

  const originalEntry = readFileSync(aliasEntry, "utf8");
  assert.match(originalEntry, /const y = x;/);
  writeFileSync(aliasEntry, originalEntry.replace("const y = x;", `const y = ${literal};`));
  const distinct = await import(`${pathToFileURL(aliasEntry)}?distinct`);
  assert.deepEqual(distinct.y, namespace.y);
  assert.notEqual(distinct.y, provider.x);
  assert.equal(split({ ...aliasRequest, checkOutput: true }).status, "refused");
}

await assertAlias("empty-array-alias", "const x=[]; export const y=x;", "[]", []);
await assertAlias("empty-object-alias", "const x={}; export const y=x;", "{}", {});

// Allocation support does not admit a read before its initializer or fill an
// array hole with a value.
for (const [name, source] of [
  ["array-before-binding", "const x=[y]; export const y=1;"],
  ["sparse-array", "const x=[,]; export const y=x;"],
]) {
  const dir = path.join(root, name);
  const { sourceFile, specFile } = prepare(dir, { source, provider: "", spec });
  const refused = split({ source: sourceFile, spec: specFile, require, out: path.join(dir, "out") });
  assert.equal(refused.reasonCode, "no_constructed_rule", refused.log);
}
