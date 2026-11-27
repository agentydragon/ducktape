import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";
import { spec } from "./gen_spec.mjs";
import { smoke } from "./smoke.mjs";

const tmp = process.env.TEST_TMPDIR;
const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const rows = [];

function inspect(source, parsed, placement) {
  const input = {
    source: dumpParsed(parsed, { includeOpaqueCaptures: true }),
    sourceText: parsed.code,
    spec: placement,
    entry: placement.entry,
  };
  // Both probes use the linked empty-profile decoder. indexedInlinePlan
  // also checks authenticated source ranges and tries the indexed inline rule.
  const [classification, inline] = JSON.parse(
    execFileSync(process.env.LINKED_INPUT_CHECKER, {
      input: JSON.stringify(["singletonSourceShape", "indexedInlinePlan"].map((kind) => ({ kind, input }))) + "\n",
      encoding: "utf8",
      maxBuffer: 64 * 1024 * 1024,
      timeout: 90_000,
    })
  );
  assert(classification.error || classification.output, source);
  assert(inline.error || inline.output, source);
  return {
    sourceClassification: classification.error ?? "decoded",
    indexedInlineProbe: inline.error ?? "constructed",
  };
}

for (const source of process.argv.slice(2)) {
  const library = path.basename(source, ".mjs");
  const code = readFileSync(source, "utf8");
  const parsed = parseModule(code, acorn.parse, eslintScope);
  const units = topLevelStatements(parsed.ast);
  const baseline = await smoke(library, source);
  assert(baseline.ok, JSON.stringify(baseline));

  // Reuse the owner boundary and priority in results/*_halves.jsonl, and
  // explicitly request that same contiguous schedule in the ordinary CLI.
  const halves = spec(units.length, "halves");
  const placement = {
    ...halves,
    order: Object.keys(halves.modules).filter((owner) => owner !== halves.entry),
    sequential: true,
  };
  const dir = path.join(artifacts, library);
  mkdirSync(dir);
  const specFile = path.join(dir, "halves-spec.json");
  writeFileSync(specFile, JSON.stringify(placement, null, 2));
  const request = { source, spec: specFile, out: path.join(tmp, library), require: "all-outcomes", timeoutMs: 90_000 };
  const result = split(request);
  if (result.status === "certified") {
    assert.equal(result.verdict.strategy, "compiled-constructor", result.log);
    assert.deepEqual(split({ ...request, checkOutput: true }).verdict, result.verdict);
    assert.deepEqual(await smoke(library, path.join(request.out, placement.entry)), baseline);
  } else {
    assert.equal(result.reasonCode, "no_constructed_rule", result.log);
    assert.equal(result.guarantee, null, result.log);
  }
  const stages = inspect(source, parsed, placement);
  const first = /^statement (\d+):/.exec(stages.sourceClassification);
  const row = {
    library,
    sha256: createHash("sha256").update(code).digest("hex"),
    units: units.length,
    strategy: "halves",
    cut: placement.modules[placement.order[0]].length,
    verdict: result.verdict,
    files: result.files,
    ...stages,
    ...(first && { firstSourceUnit: Number(first[1]), firstSourceSyntax: units[Number(first[1])].node.type }),
  };
  rows.push(row);
  console.log(JSON.stringify(row));
}
assert.equal(rows.length, 4, "expected one request per pinned library");
writeFileSync(path.join(artifacts, "workload-cohort.jsonl"), rows.map((row) => JSON.stringify(row)).join("\n") + "\n");
