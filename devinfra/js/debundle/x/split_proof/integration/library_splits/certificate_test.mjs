import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { topLevelStatements } from "../../translation/scope_metadata.mjs";
import { spec } from "./gen_spec.mjs";
import { writeDeployment } from "../../translation/testing/deployment_manifest.mjs";
import { split } from "../../translation/testing/split.mjs";
import { smoke } from "./smoke.mjs";

const results = [];
const tmp = process.env.TEST_TMPDIR;

function certificate(request) {
  const { verdict, log } = split(request);
  if (verdict.status === "certified") assert.equal(verdict.guarantee, "all-outcomes", log);
  else assert.equal(verdict.guarantee, null, log);
  return verdict;
}

function extract(n, unit) {
  return {
    entry: "entry.mjs",
    modules: {
      "entry.mjs": Array.from({ length: n }, (_, i) => i)
        .filter((i) => i !== unit)
        .map((i) => `#${i}`),
      "moved.mjs": [`#${unit}`],
    },
  };
}

function inputs(source, name, placement) {
  const spec = path.join(tmp, name + ".json");
  writeFileSync(
    spec,
    JSON.stringify({
      ...placement,
      sequential: true,
      order: Object.keys(placement.modules).filter((name) => name !== placement.entry),
    })
  );
  const out = path.join(tmp, name);
  return { manifest: writeDeployment(source, spec, out), out };
}

for (const source of process.argv.slice(2)) {
  const library = path.basename(source, ".mjs");
  const units = topLevelStatements(JSON.parse(readFileSync(source.slice(0, -4) + ".json", "utf8")));
  const n = units.length;
  const baseline = await smoke(library, source);
  assert(baseline.ok, JSON.stringify(baseline));

  const placements = [
    ["halves", spec(n, "halves")],
    ["advance-second", extract(n, 1)],
  ];
  const firstFunction = units.findIndex(({ node }, i) => i > 0 && node.type === "FunctionDeclaration");
  if (firstFunction !== -1) placements.push(["advance-function", extract(n, firstFunction)]);
  for (const [strategy, placement] of placements) {
    const request = inputs(source, library + "-" + strategy, placement);
    const verdict = certificate(request);
    if (verdict.status === "rejected") {
      assert.equal(verdict.reason_code, "no_constructed_rule", JSON.stringify(verdict));
      assert(!existsSync(request.out));
      results.push({ library, strategy, outcome: "refused", reason: verdict.reason });
    } else {
      assert.equal(verdict.strategy, "compiled-constructor");
      assert.deepEqual(certificate({ ...request, checkOutput: true }), verdict);
      assert.deepEqual(await smoke(library, path.join(request.out, placement.entry)), baseline);
      results.push({ library, strategy, outcome: "certified", scope: verdict.scope });
    }
  }
}

assert(results.length > 0, "no library placements exercised");

// A refused placement leaves no output for the caller to mistake for a split.
const source = path.join(tmp, "control.mjs");
writeFileSync(source, "var current = 1; var observed = current; export { current, observed };");
const placement = extract(3, 1);
const request = inputs(source, "early-read", placement);
const refused = certificate(request);
assert.equal(refused.status, "rejected");
assert(!existsSync(request.out));
results.push({
  library: "control",
  strategy: "advance-second",
  outcome: "refused",
  reason: refused.reason,
});

const lines = results.map((row) => JSON.stringify(row)).join("\n") + "\n";
writeFileSync(path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "certificates.jsonl"), lines);
process.stdout.write(lines);
