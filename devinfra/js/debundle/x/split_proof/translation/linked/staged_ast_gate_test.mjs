import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import path from "node:path";

const root = path.join(process.env.TEST_TMPDIR, "staged-ast-gate");
const source = path.join(root, "source.mjs");
const spec = path.join(root, "spec.json");
const host = path.join(root, "host.json");
const out = path.join(root, "out");
mkdirSync(root, { recursive: true });
const payload = "x".repeat(220_000);
writeFileSync(path.join(root, "provider.mjs"), "export {};\n");
writeFileSync(
  source,
  `import "./provider.mjs";\nlet count = 0;\nconst hostType = typeof window;\nconst payload = ${JSON.stringify(payload)};\nexport { count, hostType, payload };\n`
);
writeFileSync(spec, JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#1", "#2", "#3", "#4"] } }));
writeFileSync(host, JSON.stringify({ profile: "browser-page" }));

function certify(checkOutput) {
  const args = [writeDeployment(source, spec, out), out, "--require", "normal-completion", "--host-contract", host];
  if (checkOutput) args.push("--check-output");
  const run = spawnSync(process.env.LOWERSPLIT, args, { encoding: "utf8" });
  assert.equal(run.status, 0, run.stdout + run.stderr);
  const verdict = JSON.parse(run.stdout);
  assert.equal(verdict.status, "certified", run.stdout + run.stderr);
  assert.equal(verdict.scope, "linked-programs", run.stdout + run.stderr);
  assert.match(run.stderr, /"phase":"execution_proof"/, run.stderr);
  assert.match(run.stderr, /"phase":"ast_correspondence"/, run.stderr);
  assert.doesNotMatch(run.stderr, /"phase":"LinkedReplayInput"/, run.stderr);
  return verdict;
}

const emitted = certify(false);
assert.deepEqual(certify(true), emitted);
