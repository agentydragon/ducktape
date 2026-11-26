import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { topLevelStatements } from "../../translation/scope_metadata.mjs";
import { spec } from "./gen_spec.mjs";
import { writeDeployment } from "../../translation/testing/deployment_manifest.mjs";
import { split } from "../../translation/testing/split.mjs";
import { smoke } from "./smoke.mjs";

const results = [];
const tmp = process.env.TEST_TMPDIR;

function certificate(request) {
  const { verdict, log } = split({ ...request, structural: true });
  if (verdict.status === "certified") assert(["all-outcomes", "normal-completion"].includes(verdict.guarantee), log);
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
  writeFileSync(spec, JSON.stringify(placement));
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
      // A refusal without an observed difference is inconclusive.
      assert.equal(verdict.scope, "linked-structural");
      assert(!existsSync(request.out));
      assert.equal(strategy, "advance-second", JSON.stringify(verdict));
      results.push({ library, strategy, outcome: "unknown", reason: verdict.reason });
    } else {
      if (strategy === "advance-function") assert.equal(verdict.scope, "linked-structural-pivot");
      assert.deepEqual(certificate({ ...request, checkOutput: true }), verdict);
      assert.deepEqual(await smoke(library, path.join(request.out, placement.entry)), baseline);
      results.push({ library, strategy, outcome: "certified", scope: verdict.scope });
    }
  }
  if (library === "semver") {
    const host = path.join(tmp, "portable.json");
    writeFileSync(host, JSON.stringify({ profile: "ecmascript", stableIntrinsicValues: ["Object.defineProperty@1"] }));
    const placement = extract(n, 1);
    const request = { ...inputs(source, "semver-portable", placement), hostContract: host };
    const verdict = certificate(request);
    assert.equal(verdict.status, "certified", JSON.stringify(verdict));
    assert.equal(verdict.scope, "linked-structural-pivot");
    assert.equal(verdict.hostProfile, "ecmascript");
    assert.match(
      verdict.assumptions.join("\n"),
      /Object\.defineProperty@1:.*intrinsic\.Object\.defineProperty.*no observable effects/
    );
    assert.deepEqual(certificate({ ...request, checkOutput: true }), verdict);
    assert.deepEqual(await smoke(library, path.join(request.out, placement.entry)), baseline);
    results.push({
      library,
      strategy: "advance-second-portable",
      outcome: "certified",
      scope: verdict.scope,
      assumptions: verdict.assumptions,
    });
  }
}

assert(results.some((row) => row.strategy === "advance-function" && row.outcome === "certified"));

// Use the same advance-second placement. Both graphs finish normally, but the
// moved initializer reads the linked var cell before the entry assigns 1.
const source = path.join(tmp, "control.mjs");
writeFileSync(source, "var current = 1; var observed = current; export { current, observed };");
const placement = extract(3, 1);
const request = inputs(source, "early-read", placement);
const refused = certificate(request);
assert.equal(refused.status, "rejected");
assert(!existsSync(request.out));
const preview = path.join(tmp, "uncertified-preview");
const rendered = spawnSync(process.env.SPLIT_PREVIEW, [request.manifest, preview, "--force"], {
  encoding: "utf8",
});
if (rendered.error) throw rendered.error;
assert([0, 1].includes(rendered.status), rendered.stdout + rendered.stderr);
assert(existsSync(path.join(preview, placement.entry)), rendered.stdout + rendered.stderr);
const before = await import(pathToFileURL(source));
const after = await import(pathToFileURL(path.join(preview, placement.entry)));
assert.deepEqual(Object.keys(before), ["current", "observed"]);
assert.deepEqual(Object.keys(after), Object.keys(before));
assert.equal(before.observed, 1);
assert.equal(after.observed, undefined);
results.push({
  library: "control",
  strategy: "advance-second",
  outcome: "disproved",
  reason: refused.reason,
  witness: "observed: 1 -> undefined",
});

const lines = results.map((row) => JSON.stringify(row)).join("\n") + "\n";
writeFileSync(path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "certificates.jsonl"), lines);
process.stdout.write(lines);
