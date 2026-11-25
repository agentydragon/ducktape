import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { dump } from "../../translation/parser.mjs";
import { topLevelStatements } from "../../translation/scope_metadata.mjs";

const tmp = process.env.TEST_TMPDIR;
const out = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
assert(tmp && out);
const astFile = path.join(tmp, "ast.json");
const outputFile = path.join(out, "lowering-census.jsonl");
writeFileSync(outputFile, "");
const rows = [];
const shard = Number(process.env.TEST_SHARD_INDEX ?? 0);
const shards = Number(process.env.TEST_TOTAL_SHARDS ?? 1);
if (process.env.TEST_SHARD_STATUS_FILE) writeFileSync(process.env.TEST_SHARD_STATUS_FILE, "");
let sourceIndex = 0;
function selected() {
  return sourceIndex++ % shards === shard;
}
function record(row) {
  rows.push(row);
  appendFileSync(outputFile, `${JSON.stringify(row)}\n`);
}
function lower(profile, ast) {
  writeFileSync(astFile, JSON.stringify(ast));
  const contract = path.join(tmp, `${profile}.json`);
  writeFileSync(contract, JSON.stringify({ profile }));
  return JSON.parse(
    execFileSync(process.env.COREJS, ["--lowering-census", astFile, "--host-contract", contract], {
      encoding: "utf8",
      maxBuffer: 128 * 1024 * 1024,
    })
  );
}
// Counterfactuals diagnose later blockers, not JavaScript behavior. Treat globals as
// hypothetical module bindings and replace location-sensitive expressions with null.
function stub(ast, kind, name) {
  const copy = structuredClone(ast);
  function visit(value) {
    if (Array.isArray(value)) return value.map(visit);
    if (!value || typeof value !== "object") return value;
    if (kind === "host" && value.type === "Identifier" && value.k === "G" && value.name === name) value.k = "T";
    if (kind === value.type) return { type: "Literal", value: null, lit: "null" };
    for (const [key, child] of Object.entries(value)) value[key] = visit(child);
    return value;
  }
  return visit(copy);
}
function census(corpus, name, profile, ast) {
  const result = lower(profile, ast);
  const retries = {};
  const globals = new Set(
    result.lowering_refusals
      .filter(({ reason }) => reason.startsWith("unsupported global "))
      .map(({ reason }) => reason.slice("unsupported global ".length))
  );
  const scenarios = [
    ...[...globals].map((globalName) => [
      `host:${globalName}`,
      "host",
      globalName,
      (reason) => reason === `unsupported global ${globalName}`,
    ]),
    ["ImportExpression", "ImportExpression", "", (reason) => reason.includes("ImportExpression")],
    ["MetaProperty", "MetaProperty", "", (reason) => reason.includes("MetaProperty")],
  ];
  const units = topLevelStatements(ast).map(({ node }) => node);
  for (const [key, kind, stubName, matches] of scenarios) {
    const affected = result.lowering_refusals.filter(({ reason }) => matches(reason));
    if (!affected.length) continue;
    const ordinals = new Set(affected.map(({ unit }) => unit));
    const body = units.map((unit, i) => {
      if (unit.type === "ImportDeclaration") return unit;
      return ordinals.has(i) ? stub(unit, kind, stubName) : { type: "EmptyStatement" };
    });
    const retried = lower(profile, { ...ast, body });
    assert.equal(retried.units, affected.length, `${corpus}/${name}: ${key}`);
    assert(
      retried.lowering_refusals.every(({ unit }) => ordinals.has(unit)),
      `${corpus}/${name}: ${key}`
    );
    const errors = new Map(retried.lowering_refusals.map(({ unit, reason }) => [unit, reason]));
    retries[key] = affected.map(({ unit }) => ({ unit, reason: errors.get(unit) ?? null }));
  }
  record({ corpus, name, profile, ...result, retries });
}
function source(corpus, name, profile, file) {
  let ast;
  const code = readFileSync(file, "utf8");
  try {
    ast = dump(code, { includeOpaqueCaptures: true });
  } catch (error) {
    record({ corpus, name, profile, error: error.message });
    return;
  }
  census(corpus, name, profile, ast);
}
const corpusRoot = path.join(path.dirname(process.env.CORPUS_MANIFEST), "cases");
const manifest = JSON.parse(readFileSync(process.env.CORPUS_MANIFEST, "utf8"));
const caseDirs = manifest.filter(({ path: p }) => p.endsWith("/expect.yaml")).map(({ path: p }) => path.dirname(p));
for (const dir of caseDirs) {
  const list = path.join(corpusRoot, dir, "js-files.txt");
  const files = existsSync(list)
    ? readFileSync(list, "utf8").split(/\r?\n/).filter(Boolean)
    : manifest
        .map(({ path: p }) => p)
        .filter((p) => p.startsWith(`${dir}/`) && p.endsWith(".js"))
        .map((p) => p.slice(dir.length + 1));
  for (const file of files) {
    if (selected()) source("debundle", `${dir}/${file}`, "empty", path.join(corpusRoot, dir, file));
  }
}
const libraries = path.join(tmp, "libraries");
mkdirSync(libraries);
execFileSync(process.env.LIBRARY_PREPARE, [libraries], { stdio: "inherit" });
for (const file of readdirSync(libraries)
  .filter((p) => p.endsWith(".mjs"))
  .sort()) {
  if (selected()) source("libraries", file, "ecmascript", path.join(libraries, file));
}
const bundle = process.env.EXCALIDRAW_OUTPUTS.split(" ").find((p) => p.endsWith("/bundle"));
assert(bundle);
for (const file of readdirSync(bundle, { recursive: true })
  .filter((p) => p.endsWith(".js"))
  .sort()) {
  if (selected()) source("excalidraw", file, "browser-page", path.join(bundle, file));
}
console.log(`Lowered ${rows.length} files; ${rows.filter((row) => row.error).length} failed`);
