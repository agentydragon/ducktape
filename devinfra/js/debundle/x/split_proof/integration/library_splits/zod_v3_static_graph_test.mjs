import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parse } from "acorn";
import { split } from "../../translation/testing/split.mjs";

const entry = "v3/index.js";
const locale = "v3/locales/en.js";
const worker = "v3/locales/en-error-map.mjs";
// In source order, including the independent import and re-export of the same target.
const graph = {
  [entry]: ["./external.js", "./external.js"],
  "v3/external.js": [
    "./errors.js",
    "./helpers/parseUtil.js",
    "./helpers/typeAliases.js",
    "./helpers/util.js",
    "./types.js",
    "./ZodError.js",
  ],
  "v3/errors.js": ["./locales/en.js"],
  "v3/helpers/parseUtil.js": ["../errors.js", "../locales/en.js"],
  "v3/helpers/typeAliases.js": [],
  "v3/helpers/util.js": [],
  "v3/types.js": [
    "./ZodError.js",
    "./errors.js",
    "./helpers/errorUtil.js",
    "./helpers/parseUtil.js",
    "./helpers/util.js",
  ],
  "v3/ZodError.js": ["./helpers/util.js"],
  "v3/helpers/errorUtil.js": [],
  [locale]: ["../ZodError.js", "../helpers/util.js"],
};
const files = ["package.json", "v3/package.json", ...Object.keys(graph)];
const packageRoot = path.dirname(createRequire(import.meta.url).resolve("zod/package.json"));
const fixtureFile = (name) => fileURLToPath(new URL(`./zod_v3_static_graph/${name}`, import.meta.url));
const tmp = mkdtempSync(path.join(process.env.TEST_TMPDIR, "zod-v3-"));
const read = (root, file) => readFileSync(path.join(root, file), "utf8");
const ast = (text) => parse(text, { ecmaVersion: "latest", sourceType: "module" });

function write(root, file, bytes) {
  const dest = path.join(root, file);
  mkdirSync(path.dirname(dest), { recursive: true });
  writeFileSync(dest, bytes);
}

function assertGraph(root, expected) {
  const visited = new Set();
  function visit(file) {
    if (visited.has(file)) return;
    visited.add(file);
    const imports = ast(read(root, file))
      .body.filter(({ source }) => source)
      .map(({ source }) => source.value);
    assert.deepEqual(imports, expected[file], file);
    for (const specifier of imports) visit(path.posix.join(path.posix.dirname(file), specifier));
  }
  visit(entry);
  assert.deepEqual([...visited].sort(), Object.keys(expected).sort());
}

function stage(name, edits = {}) {
  const dir = path.join(tmp, name);
  const root = path.join(dir, "node_modules/zod");
  for (const file of files) write(root, file, readFileSync(path.join(packageRoot, file)));
  for (const [file, bytes] of Object.entries(edits)) write(root, file, bytes);
  const consumer = path.join(dir, "consumer.mjs");
  copyFileSync(fixtureFile("consumer.mjs"), consumer);
  return { root, consume: async () => (await import(pathToFileURL(consumer).href)).observation };
}

const original = stage("original");
const pkg = JSON.parse(read(original.root, "package.json"));
assert.equal(pkg.version, "4.6.7");
assert.equal(pkg.type, "module");
assert.equal(pkg.exports["./v3"].import, "./v3/index.js");
assert.equal(JSON.parse(read(original.root, "v3/package.json")).type, "module");
assertGraph(original.root, graph);
const code = read(original.root, locale);
assert.equal(
  createHash("sha256").update(code).digest("hex"),
  "51a6dcc5d4bd52a64da127c012c6c963395c1d9a343ad75d4a8eca662fc8b205"
);
const rows = ast(code).body;
assert.deepEqual(
  rows.map(({ type }) => type),
  ["ImportDeclaration", "ImportDeclaration", "VariableDeclaration", "ExportDefaultDeclaration"]
);
const placement = JSON.parse(readFileSync(fixtureFile("split.json"), "utf8"));
assert.deepEqual(placement, {
  entry: locale,
  modules: { [worker]: ["#0", "#1", "#2"], [locale]: ["#3"] },
  order: [worker],
  sequential: true,
});
const [, utilImport, definition, snapshot] = rows;
assert.equal(definition.kind, "const");
assert.equal(definition.declarations[0].id.name, "errorMap");
assert.equal(snapshot.declaration.name, "errorMap");
const snapshotSource = code.slice(snapshot.start);
const ownerSource = code.slice(0, definition.start) + "export " + code.slice(definition.start, snapshot.start);
const reference = {
  [locale]: 'import { errorMap } from "./en-error-map.mjs";\n' + snapshotSource,
  [worker]: ownerSource,
};

const manifest = path.join(tmp, "deployment.json");
write(
  tmp,
  "deployment.json",
  JSON.stringify({
    entry,
    chunks: Object.keys(graph).map((file) => ({
      path: file,
      source: path.join(original.root, file),
      ...(file === locale ? { spec: fixtureFile("split.json") } : {}),
    })),
  })
);
const out = path.join(tmp, "cli-output");
function assertRefusal(result) {
  assert.equal(result.status, "refused", result.log);
  assert.equal(result.reasonCode, "no_constructed_rule", result.log);
  assert.equal(result.reason, "no constructor supports multi-chunk deployments", result.log);
}

const verdict = split({ manifest, out });
assertRefusal(verdict);
const staged = stage("reference", reference);
const reread = split({ manifest, out: staged.root, checkOutput: true });
assertRefusal(reread);
const splitGraph = { ...graph, [locale]: ["./en-error-map.mjs"], [worker]: graph[locale] };
assertGraph(staged.root, splitGraph);
for (const file of files.filter((file) => file !== locale)) {
  assert.deepEqual(readFileSync(path.join(staged.root, file)), readFileSync(path.join(original.root, file)), file);
}

const baseline = await original.consume();
const candidate = await staged.consume();
assert(baseline.namespace.keys.includes("defaultErrorMap"));
assert(baseline.namespace.aliased);
assert.deepEqual(candidate.namespace, baseline.namespace);
assert.deepEqual(candidate.behavior, baseline.behavior);
const invalidType = (message) => [{ code: "invalid_type", path: ["label"], message }];
assert.deepEqual(baseline.behavior, {
  initialMap: true,
  missing: invalidType("Required"),
  wrongType: invalidType("Expected string, received number"),
  short: [{ code: "too_small", path: ["label"], message: "String must contain at least 3 character(s)" }],
  invalidEnum: "Invalid enum value. Expected 'short' | 'long', received 'other'",
  installedMap: true,
  overridden: invalidType("form: Required"),
  restored: invalidType("Required"),
  valid: { label: "long" },
});

// Put the snapshot in the owner before the lexical initializer, routing the
// same default name through the facade. The dependency graph is unchanged.
const earlyRead = stage("early-read", {
  [locale]: 'export { default } from "./en-error-map.mjs";\n',
  [worker]: code.slice(0, definition.start) + snapshotSource + code.slice(definition.start, snapshot.start),
});
assertGraph(earlyRead.root, splitGraph);
await assert.rejects(earlyRead.consume(), { name: "ReferenceError", message: /errorMap.*before initialization/ });

// Use another real export from the same provider, under the same local name.
// All routes and public names are unchanged, but the undefined sentinel is lost.
const parsedTypeImport = utilImport.specifiers.find(({ local }) => local.name === "ZodParsedType");
const wrongCell = stage("wrong-cell", {
  ...reference,
  [worker]:
    ownerSource.slice(0, parsedTypeImport.start) + "util as ZodParsedType" + ownerSource.slice(parsedTypeImport.end),
});
assertGraph(wrongCell.root, splitGraph);
const wrong = await wrongCell.consume();
assert.deepEqual(wrong.namespace, baseline.namespace);
assert.deepEqual(wrong.behavior.missing, invalidType("Expected string, received undefined"));
assert.deepEqual(wrong.behavior.wrongType, baseline.behavior.wrongType);
assert.deepEqual(wrong.behavior.overridden, invalidType("form: Expected string, received undefined"));

console.log(
  JSON.stringify({
    workload: "zod@4.6.7 / zod/v3",
    node: process.versions.node,
    publicKeys: baseline.namespace.keys.length,
    behavior: candidate.behavior,
    wrongCell: wrong.behavior.missing,
    wrongOrder: "ReferenceError",
    ordinary: verdict.verdict,
    checkOutput: reread.verdict,
  })
);

if (process.argv.includes("--linked-diagnostic")) {
  const { inspect } = await import("./zod_v3_static_graph/diagnostic.mjs");
  inspect({
    original: original.root,
    candidate: staged.root,
    manifest,
    worker,
  });
}
