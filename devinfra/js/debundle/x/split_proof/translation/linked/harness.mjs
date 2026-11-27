import assert from "node:assert/strict";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
import { parse } from "acorn";
import { split } from "../testing/split.mjs";
import { generate } from "astring";

export const root = process.env.TEST_TMPDIR;
assert(root, "TEST_TMPDIR is required");
export function caseShard() {
  const count = Number(process.env.TEST_TOTAL_SHARDS ?? 1);
  const index = Number(process.env.TEST_SHARD_INDEX ?? 0);
  assert(Number.isInteger(count) && count > 0, "invalid TEST_TOTAL_SHARDS");
  assert(Number.isInteger(index) && index >= 0 && index < count, "invalid TEST_SHARD_INDEX");
  if (process.env.TEST_SHARD_STATUS_FILE) writeFileSync(process.env.TEST_SHARD_STATUS_FILE, "");

  // Every shard visits the same case names in the same order.
  const names = new Set();
  return (name) => {
    assert(!names.has(name), `duplicate case ${name}`);
    const selected = names.size % count === index;
    names.add(name);
    if (selected) console.log(`Case ${name}`);
    return selected;
  };
}

export const id = (name) => ({ type: "Identifier", name });
export const literal = (value) => ({ type: "Literal", value });
export const importNamed = (source, bindings) => ({
  type: "ImportDeclaration",
  source: literal(source),
  specifiers: bindings.map(([imported, local]) => ({
    type: "ImportSpecifier",
    imported: id(imported),
    local: id(local),
  })),
});
export const exportNamed = (bindings) => ({
  type: "ExportNamedDeclaration",
  declaration: null,
  specifiers: bindings.map(([local, exported]) => ({
    type: "ExportSpecifier",
    local: id(local),
    exported: id(exported),
  })),
  source: null,
});
export const program = (body) => generate({ type: "Program", sourceType: "module", body });

const probe = path.join(root, "linked_programs_observe.mjs");
writeFileSync(
  probe,
  `try {
  const module = await import(process.argv[2]);
  const names = JSON.parse(process.argv[3]);
  const values = Object.fromEntries(names.map((name) => {
    const exported = module[name];
    const value = typeof exported === "function" ? exported() : exported;
    return [name, typeof value === "function" ? value() : value];
  }));
  console.log(JSON.stringify({ values }));
} catch (error) {
  console.log(JSON.stringify({ error: error.name }));
}
`
);

export function observe(file, exportNames) {
  const names = Array.isArray(exportNames) ? exportNames : [exportNames];
  const result = spawnSync(process.execPath, [probe, pathToFileURL(file).href, JSON.stringify(names)], {
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.error?.message ?? result.stdout + result.stderr);
  const observed = JSON.parse(result.stdout.trim());
  if (observed.error) return observed;
  return Object.fromEntries(names.map((name) => [name, observed.values[name]]));
}

export function lower(source, spec, out, extra = [], scope = null) {
  const options = { source, spec, out };
  for (let i = 0; i < extra.length; i++) {
    const flag = extra[i];
    if (flag === "--check-output") options.checkOutput = true;
    else if (flag === "--require") options.require = extra[++i];
    else throw new Error(`unsupported certified split option: ${flag}`);
  }
  const result = split(options);
  if (process.env.SPLIT_PROOF_TIMINGS === "1") process.stderr.write(result.stderr);
  if (result.status === "certified") assert.equal(result.verdict.strategy, "compiled-constructor", result.log);
  if (scope !== null) assert.equal(result.scope, scope, result.log);
  return { verdict: result.verdict, log: result.log };
}

export function prepare(dir, { source, provider, extras = {}, sourceName = "source.mjs", spec }) {
  mkdirSync(dir, { recursive: true });
  const sourceFile = path.join(dir, sourceName);
  const providerFile = path.join(dir, "provider.mjs");
  const specFile = path.join(dir, "spec.json");
  writeFileSync(sourceFile, source);
  writeFileSync(providerFile, provider);
  for (const [name, contents] of Object.entries(extras)) writeFileSync(path.join(dir, name), contents);
  writeFileSync(specFile, JSON.stringify(spec, null, 2));
  return { sourceFile, providerFile, specFile };
}

// Compute evaluation order from the emitted requests, including the cycle a fixed
// schedule can introduce when the second provider reads the first provider.
export function moduleEvaluationOrder(directory, entry) {
  const visited = new Set();
  const order = [];
  function visit(file) {
    if (visited.has(file)) return;
    visited.add(file);
    const ast = parse(readFileSync(path.join(directory, file), "utf8"), {
      sourceType: "module",
      ecmaVersion: "latest",
    });
    for (const node of ast.body) {
      if (node.type === "ImportDeclaration") visit(path.join(path.dirname(file), node.source.value));
    }
    order.push(file);
  }
  visit(entry);
  return order;
}

export function runSplit(name, fixture, scope = null) {
  const dir = path.join(root, name);
  const files = prepare(dir, fixture);
  const out = path.join(dir, "out");
  return { dir, files, out, ...lower(files.sourceFile, files.specFile, out, [], scope) };
}

const entry = "entry.mjs";
const worker = "worker.mjs";
const workerModules = { [worker]: ["#0"], [entry]: ["#1", "#2"] };

export function sequentialSplit(name, source, modules = workerModules, order = [worker], provider = "", scope = null) {
  return runSplit(name, { source, provider, spec: { entry, modules, order, sequential: true } }, scope);
}

export function assertCertified(verdict, log) {
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "all-outcomes", log);
}

export function assertCertifiedResult({ files, out, verdict, log }, result) {
  assertCertified(verdict, log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result }, file);
  }
}
