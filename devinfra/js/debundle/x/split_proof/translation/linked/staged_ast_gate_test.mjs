import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
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
  const args = [
    writeDeployment(source, spec, out),
    out,
    "--require",
    "normal-completion",
    "--host-contract",
    host,
    "--structural",
  ];
  if (checkOutput) args.push("--check-output");
  // The compiled checker must not depend on ambient Lean paths.
  const run = spawnSync(process.env.LOWERSPLIT, args, {
    encoding: "utf8",
    env: {
      ...process.env,
      LEAN_PATH: path.join(root, "untrusted-imports"),
      LEAN_SRC_PATH: path.join(root, "untrusted-sources"),
      LEAN_SYSROOT: path.join(root, "untrusted-sdk"),
    },
  });
  assert.equal(run.status, 0, run.stdout + run.stderr);
  const verdict = JSON.parse(run.stdout);
  assert.equal(verdict.status, "certified", run.stdout + run.stderr);
  assert.equal(verdict.scope, "linked-structural-order", run.stdout + run.stderr);
  assert.match(run.stderr, /"phase":"output_comparison"/, run.stderr);
  assert.match(run.stderr, /"phase":"structural_certificate"/, run.stderr);
  assert.doesNotMatch(run.stderr, /"phase":"(?:execution_proof|ast_correspondence|LinkedReplayInput)"/, run.stderr);
  return verdict;
}

const emitted = certify(false);
assert.deepEqual(certify(true), emitted);

// The disk gate rejects a changed file and leaves the supplied tree untouched.
const emittedFile = path.join(out, "entry.mjs");
const tampered = readFileSync(emittedFile, "utf8").replace("count = 0", "count = 1");
assert.notEqual(tampered, readFileSync(emittedFile, "utf8"));
writeFileSync(emittedFile, tampered);
const check = spawnSync(
  process.env.LOWERSPLIT,
  [
    writeDeployment(source, spec, out),
    out,
    "--require",
    "normal-completion",
    "--host-contract",
    host,
    "--structural",
    "--check-output",
  ],
  { encoding: "utf8" }
);
assert.equal(check.status, 1, check.stdout + check.stderr);
assert.equal(JSON.parse(check.stdout).status, "rejected", check.stdout + check.stderr);
assert.equal(readFileSync(emittedFile, "utf8"), tampered);
