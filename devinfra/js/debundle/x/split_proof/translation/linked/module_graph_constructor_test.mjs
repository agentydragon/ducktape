import assert from "node:assert/strict";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { parse } from "acorn";
import { generate } from "astring";
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
