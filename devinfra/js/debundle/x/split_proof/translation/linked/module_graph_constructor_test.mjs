import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parse } from "acorn";
import { generate } from "astring";
import { dump } from "../parser.mjs";
import { split } from "../testing/split.mjs";
import { moduleGraphFixture, sparseNonterminalFixture } from "./module_graph_fixture.mjs";

const root = process.env.TEST_TMPDIR;
const fixture = moduleGraphFixture();
const source = path.join(root, "source.mjs");
const spec = path.join(root, "spec.json");
const out = path.join(root, "out");
writeFileSync(source, fixture.source);
writeFileSync(spec, JSON.stringify(fixture.spec));

const produced = split({ source, spec, out, require: "all-outcomes", timeoutMs: 90_000 });
assert.equal(produced.status, "certified", produced.log);
assert.equal(produced.scope, "indexed-local-exports");
assert.equal(produced.verdict.strategy, "compiled-constructor");
const checked = split({ source, spec, out, require: "all-outcomes", checkOutput: true, timeoutMs: 90_000 });
assert.equal(checked.status, "certified", checked.log);
assert.equal(checked.scope, "indexed-local-exports");
assert.equal(checked.verdict.strategy, "compiled-constructor");

const namespace = await import(pathToFileURL(path.join(out, fixture.entry)).href);
assert.deepEqual(Object.fromEntries(fixture.names.map((name) => [name, namespace[name]])), fixture.expected);

// reader0 and data0 have the same value but are distinct live cells. Replacing
// the consumer's exported cell must fail the ordinary output readback check.
const consumer = path.join(out, "consumer0.mjs");
const before = readFileSync(consumer, "utf8");
const ast = parse(before, { sourceType: "module", ecmaVersion: "latest" });
const imports = ast.body.flatMap((stmt) => (stmt.type === "ImportDeclaration" ? stmt.specifiers : []));
const exports = ast.body.flatMap((stmt) => (stmt.type === "ExportNamedDeclaration" ? stmt.specifiers : []));
const reader = exports.find((item) => item.exported.name === "reader0");
assert(imports.some((item) => item.local.name === "data0"));
assert.equal(reader?.local.name, "reader0");
assert.equal(fixture.expected.reader0, "value0");
reader.local.name = "data0";
const altered = generate(ast);
writeFileSync(consumer, altered);
const wrongCell = split({ source, spec, out, require: "all-outcomes", checkOutput: true, timeoutMs: 90_000 });
assert.equal(wrongCell.status, "refused", wrongCell.log);
assert.equal(readFileSync(consumer, "utf8"), altered, "readback refusal changed existing bytes");
writeFileSync(consumer, before);

// Request a later owner for the first provider, changing the original order.
const wrongOwner = structuredClone(fixture.spec);
wrongOwner.modules["provider0.mjs"] = wrongOwner.modules["provider0.mjs"].filter((id) => id !== "#0");
wrongOwner.modules["consumer0.mjs"].unshift("#0");
const wrongSpec = path.join(root, "wrong-owner.json");
const protectedOut = path.join(root, "protected");
writeFileSync(wrongSpec, JSON.stringify(wrongOwner));
mkdirSync(protectedOut);
const sentinel = path.join(protectedOut, "sentinel.txt");
writeFileSync(sentinel, "preserve existing bytes\n");
const ownerResult = split({
  source,
  spec: wrongSpec,
  out: protectedOut,
  require: "all-outcomes",
  allowExistingOut: true,
  timeoutMs: 90_000,
});
assert.equal(ownerResult.status, "refused", ownerResult.log);
assert.equal(readFileSync(sentinel, "utf8"), "preserve existing bytes\n");

// The original #32 metadata unit must be selected once, not omitted or
// repeated within the same authored module. A refusal must not publish files.
for (const [kind, entrySelectors] of [
  ["duplicate", ["#32", "#32"]],
  ["dropped", []],
]) {
  const invalid = structuredClone(fixture.spec);
  invalid.modules[fixture.entry] = entrySelectors;
  const invalidSpec = path.join(root, `${kind}-metadata.json`);
  writeFileSync(invalidSpec, JSON.stringify(invalid));
  const result = split({
    source,
    spec: invalidSpec,
    out: protectedOut,
    require: "all-outcomes",
    allowExistingOut: true,
    timeoutMs: 90_000,
  });
  assert.equal(result.status, "refused", result.log);
  assert.equal(readFileSync(sentinel, "utf8"), "preserve existing bytes\n");
  assert.deepEqual(readdirSync(protectedOut), ["sentinel.txt"]);
}

console.log(
  JSON.stringify({
    fixture: "module_graph_test/16",
    originalExportOrdinal: 32,
    strategy: produced.verdict.strategy,
    emittedAndReadBack: "green",
    wrongOwner: ownerResult.status,
    equalValueWrongCell: wrongCell.status,
  })
);

function probe(kind, input) {
  const request = [{ kind, input }];
  const result = spawnSync(process.env.LINKED_INPUT_CHECKER, [], {
    encoding: "utf8",
    input: JSON.stringify(request) + "\n",
    maxBuffer: 64 * 1024 * 1024,
    timeout: 90_000,
  });
  if (result.error) throw result.error;
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout)[0];
}

function namedProbe(placement, sourceText = fixture.source) {
  return probe("namedConstructor", { source: dump(sourceText), spec: placement });
}

// Reuse the sparse_checked_artifact_test.mjs sample: the export is not terminal.
// #2 is the actual result declaration; #1 does not execute or allocate a region.
const { source: sparseSource, spec: sparseSpec } = sparseNonterminalFixture();
const shape = probe("singletonSourceShape", { source: dump(sparseSource), entry: sparseSpec.entry });
assert.deepEqual(shape.output, {
  entry: "entry.mjs",
  modules: [
    {
      name: "entry.mjs",
      body: [
        [0, ["seed"]],
        [2, ["result"]],
      ],
      exports: [["exposed", "seed"]],
    },
  ],
});
const sparseConstructor = namedProbe(sparseSpec, sparseSource);
assert.match(sparseConstructor.error, /source position 1 has no executable unit/);
const sparseSourceFile = path.join(root, "sparse-source.mjs");
const sparseSpecFile = path.join(root, "sparse-spec.json");
const sparseOut = path.join(root, "sparse-out");
writeFileSync(sparseSourceFile, sparseSource);
writeFileSync(sparseSpecFile, JSON.stringify(sparseSpec));
const sparse = split({ source: sparseSourceFile, spec: sparseSpecFile, out: sparseOut, require: "all-outcomes" });
assert.equal(sparse.status, "certified", sparse.log);
assert.equal(sparse.scope, "indexed-local-exports", sparse.log);
assert.equal(sparse.verdict.strategy, "compiled-constructor", sparse.log);
assert.equal(sparse.guarantee, "all-outcomes", sparse.log);
const sparseChecked = split({
  source: sparseSourceFile,
  spec: sparseSpecFile,
  out: sparseOut,
  require: "all-outcomes",
  checkOutput: true,
});
assert.equal(sparseChecked.status, "certified", sparseChecked.log);
assert.equal(sparseChecked.scope, "indexed-local-exports", sparseChecked.log);
assert.equal(sparseChecked.verdict.strategy, "compiled-constructor", sparseChecked.log);
const sparseEntry = path.join(sparseOut, sparseSpec.entry);
const sparseNamespace = await import(pathToFileURL(sparseEntry).href);
assert.equal(sparseNamespace.exposed, 42);

// `seed` and `result` have the same value but are different live cells.
const sparseBefore = readFileSync(sparseEntry, "utf8");
const sparseAst = parse(sparseBefore, { sourceType: "module", ecmaVersion: "latest" });
const sparseExport = sparseAst.body
  .flatMap((stmt) => (stmt.type === "ExportNamedDeclaration" ? stmt.specifiers : []))
  .find((item) => item.exported.name === "exposed");
assert.equal(sparseExport?.local.name, "seed");
sparseExport.local.name = "result";
const sparseAltered = generate(sparseAst);
writeFileSync(sparseEntry, sparseAltered);
const sparseWrongCell = split({
  source: sparseSourceFile,
  spec: sparseSpecFile,
  out: sparseOut,
  require: "all-outcomes",
  checkOutput: true,
});
assert.equal(sparseWrongCell.status, "refused", sparseWrongCell.log);
assert.equal(readFileSync(sparseEntry, "utf8"), sparseAltered);
writeFileSync(sparseEntry, sparseBefore);

const sparseWrongOwner = structuredClone(sparseSpec);
sparseWrongOwner.modules["worker.mjs"].push("#1");
sparseWrongOwner.modules["entry.mjs"] = ["#2"];
const sparseWrongSpec = path.join(root, "sparse-wrong-owner.json");
const sparseProtected = path.join(root, "sparse-protected");
writeFileSync(sparseWrongSpec, JSON.stringify(sparseWrongOwner));
mkdirSync(sparseProtected);
const sparseSentinel = path.join(sparseProtected, "sentinel.txt");
writeFileSync(sparseSentinel, "preserve existing bytes\n");
const sparseOwnerResult = split({
  source: sparseSourceFile,
  spec: sparseWrongSpec,
  out: sparseProtected,
  require: "all-outcomes",
  allowExistingOut: true,
});
assert.equal(sparseOwnerResult.status, "refused", sparseOwnerResult.log);
assert.equal(readFileSync(sparseSentinel, "utf8"), "preserve existing bytes\n");

// Repeating #1 in the same module previously disappeared into the planner's
// set. It must be refused before either guarantee publishes an output tree.
const sparseDuplicate = structuredClone(sparseSpec);
sparseDuplicate.modules[sparseSpec.entry].push("#1");
const sparseDuplicateSpec = path.join(root, "sparse-duplicate.json");
writeFileSync(sparseDuplicateSpec, JSON.stringify(sparseDuplicate));
for (const guarantee of ["normal-completion", "all-outcomes"]) {
  const duplicateOut = path.join(root, `sparse-duplicate-${guarantee}`);
  const duplicateResult = split({
    source: sparseSourceFile,
    spec: sparseDuplicateSpec,
    out: duplicateOut,
    require: guarantee,
  });
  assert.equal(duplicateResult.status, "refused", duplicateResult.log);
  assert.match(duplicateResult.reason, /each metadata row must be selected exactly once/);
  assert.equal(existsSync(duplicateOut), false, "duplicate metadata created an output directory");
}
console.log(
  JSON.stringify({
    fixture: "sparse-interior-export",
    shape: shape.output,
    constructor: sparseConstructor,
    production: { status: sparse.status, scope: sparse.scope, strategy: sparse.verdict.strategy },
    wrongCell: sparseWrongCell.status,
    wrongOwner: sparseOwnerResult.status,
  })
);

// A CoreJS undefined initializer must survive independent output lowering
// exactly, including through an imported live cell.
const undefSource = path.join(root, "undefined-source.mjs");
const undefSpec = path.join(root, "undefined-spec.json");
const undefOut = path.join(root, "undefined-out");
writeFileSync(undefSource, "const first = undefined;\nconst second = first;\nexport { second };\n");
writeFileSync(
  undefSpec,
  JSON.stringify({
    entry: "entry.mjs",
    modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
    order: ["worker.mjs"],
    sequential: true,
  })
);
const undefProduced = split({ source: undefSource, spec: undefSpec, out: undefOut, require: "all-outcomes" });
assert.equal(undefProduced.status, "certified", undefProduced.log);
assert.equal(undefProduced.scope, "indexed-local-exports", undefProduced.log);
assert.equal(undefProduced.verdict.strategy, "compiled-constructor", undefProduced.log);
const undefChecked = split({
  source: undefSource,
  spec: undefSpec,
  out: undefOut,
  require: "all-outcomes",
  checkOutput: true,
});
assert.equal(undefChecked.status, "certified", undefChecked.log);
assert.equal(undefChecked.scope, "indexed-local-exports", undefChecked.log);
assert.equal((await import(pathToFileURL(path.join(undefOut, "entry.mjs")).href)).second, undefined);

// A local binding named undefined is safe when this module has no CoreJS
// undefined expression to print. Keep this previously accepted shape.
const shadowSource = path.join(root, "shadow-source.mjs");
const shadowOut = path.join(root, "shadow-out");
writeFileSync(shadowSource, "const undefined = 3;\nconst second = undefined;\nexport { second };\n");
const shadowProduced = split({ source: shadowSource, spec: undefSpec, out: shadowOut, require: "all-outcomes" });
assert.equal(shadowProduced.status, "certified", shadowProduced.log);
assert.equal(shadowProduced.scope, "indexed-local-exports", shadowProduced.log);
assert.equal(shadowProduced.verdict.strategy, "compiled-constructor", shadowProduced.log);
const shadowChecked = split({
  source: shadowSource,
  spec: undefSpec,
  out: shadowOut,
  require: "all-outcomes",
  checkOutput: true,
});
assert.equal(shadowChecked.status, "certified", shadowChecked.log);
assert.equal((await import(pathToFileURL(path.join(shadowOut, "entry.mjs")).href)).second, 3);
