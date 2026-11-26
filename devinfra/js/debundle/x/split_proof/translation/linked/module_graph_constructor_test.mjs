import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parse } from "acorn";
import { generate } from "astring";
import { dump } from "../parser.mjs";
import { split } from "../testing/split.mjs";
import { moduleGraphFixture } from "./module_graph_fixture.mjs";

const root = process.env.TEST_TMPDIR;
const fixture = moduleGraphFixture();
const source = path.join(root, "source.mjs");
const spec = path.join(root, "spec.json");
const out = path.join(root, "out");
writeFileSync(source, fixture.source);
writeFileSync(spec, JSON.stringify(fixture.spec));

const produced = split({ source, spec, out, require: "all-outcomes", timeoutMs: 90_000 });
assert.equal(produced.status, "certified", produced.log);
assert.equal(produced.scope, "terminal-declarations");
assert.equal(produced.verdict.strategy, "compiled-constructor");
const checked = split({ source, spec, out, require: "all-outcomes", checkOutput: true, timeoutMs: 90_000 });
assert.equal(checked.status, "certified", checked.log);
assert.equal(checked.scope, "terminal-declarations");
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
const sparseSource = "const seed = 42;\nexport { seed as exposed };\nconst result = seed;\n";
const sparseSpec = {
  entry: "entry.mjs",
  modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
  order: ["worker.mjs"],
  sequential: true,
};
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
assert.equal(sparse.status, "refused", sparse.log);
assert(!existsSync(sparseOut), "a refused sparse split must not publish output");
console.log(
  JSON.stringify({
    fixture: "sparse-interior-export",
    shape: shape.output,
    constructor: sparseConstructor,
    production: { status: sparse.status, scope: sparse.scope, reason: sparse.reason },
  })
);
