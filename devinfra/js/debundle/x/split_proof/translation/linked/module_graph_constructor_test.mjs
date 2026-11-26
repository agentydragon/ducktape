import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
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

function run(placement, dest) {
  writeFileSync(spec, JSON.stringify(placement));
  try {
    const result = split({ source, spec, out: dest, require: "all-outcomes", timeoutMs: 90_000 });
    return {
      status: result.status,
      scope: result.scope,
      strategy: result.verdict.strategy ?? null,
      reason: result.reason,
    };
  } catch (error) {
    if (error.code === "ETIMEDOUT") return { status: "blocked", reason: "CLI exceeded 90 seconds" };
    throw error;
  }
}

const constructor = namedProbe(fixture.spec);
const production = run(fixture.spec, out);
const exactStrategy = production.status === "certified" && production.strategy === "compiled-constructor";
const outcome = exactStrategy ? "green" : production.status === "blocked" ? "blocked" : "refused";
let wrongCell = "blocked: no constructor output to mutate";
if (exactStrategy) {
  assert.equal(constructor.error, undefined);
  assert.equal(production.scope, "named-declarations");
  writeFileSync(spec, JSON.stringify(fixture.spec));
  assert.equal(split({ source, spec, out, require: "all-outcomes", checkOutput: true }).status, "certified");
  const entry = path.join(out, fixture.entry);
  const before = readFileSync(entry, "utf8");
  const ast = parse(before, { sourceType: "module", ecmaVersion: "latest" });
  const exports = ast.body.flatMap((stmt) => (stmt.type === "ExportNamedDeclaration" ? stmt.specifiers : []));
  const first = exports.find((item) => item.exported.name === "reader0");
  const second = exports.find((item) => item.exported.name === "reader1");
  assert(first && second && first.local.name !== second.local.name, "entry must expose distinct cells");
  first.local.name = second.local.name;
  writeFileSync(entry, generate(ast));
  const checked = split({ source, spec, out, require: "all-outcomes", checkOutput: true });
  assert.equal(checked.status, "refused");
  writeFileSync(entry, before);
  wrongCell = "refused";
} else {
  assert(constructor.error, "a legacy certificate cannot establish constructor acceptance");
}

// Move a provider into a later consumer, changing its requested owner and schedule.
const wrongOwner = structuredClone(fixture.spec);
wrongOwner.modules["provider0.mjs"] = wrongOwner.modules["provider0.mjs"].filter((id) => id !== "#0");
wrongOwner.modules["consumer0.mjs"].unshift("#0");
const wrongProbe = namedProbe(wrongOwner);
const wrongOutput = path.join(root, "wrong-owner");
const wrongProduction = run(wrongOwner, wrongOutput);
assert(wrongProbe.error, "constructor accepted the wrong owner");
assert.notEqual(wrongProduction.strategy, "compiled-constructor", JSON.stringify(wrongProduction));
if (wrongProduction.status === "refused") assert(!existsSync(wrongOutput));

// Without a constructor-produced candidate there is no output cell to mutate.
console.log(
  JSON.stringify({
    fixture: "module_graph_test/16",
    originalExportOrdinal: 32,
    constructor,
    production,
    outcome,
    wrongOwner: { constructor: wrongProbe, production: wrongProduction },
    wrongCell,
  })
);

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
