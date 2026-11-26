import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import path from "node:path";

const root = path.join(process.env.TEST_TMPDIR, "staged-ast-gate");
const source = path.join(root, "source.mjs");
const spec = path.join(root, "spec.json");
const host = path.join(root, "host.json");
const out = path.join(root, "out");
mkdirSync(root, { recursive: true });
const fakeBin = path.join(root, "bin");
const leanMarker = path.join(root, "lean-invoked");
mkdirSync(fakeBin);
writeFileSync(path.join(fakeBin, "lean"), '#!/usr/bin/env bash\nprintf attempted > "$FAKE_LEAN_MARKER"\nexit 97\n', {
  mode: 0o755,
});
const env = {
  ...process.env,
  PATH: `${fakeBin}:${process.env.PATH ?? ""}`,
  FAKE_LEAN_MARKER: leanMarker,
  LEAN_PATH: path.join(root, "untrusted-imports"),
  LEAN_SRC_PATH: path.join(root, "untrusted-sources"),
  LEAN_SYSROOT: path.join(root, "untrusted-sdk"),
};

function run(sourceFile, specFile, dest, args = []) {
  const manifest = writeDeployment(sourceFile, specFile, dest);
  const result = spawnSync(process.env.LOWERSPLIT, [manifest, dest, ...args], { encoding: "utf8", env });
  assert.ifError(result.error);
  const verdict = JSON.parse(result.stdout);
  assert.equal(result.status, verdict.status === "certified" ? 0 : 1, result.stdout + result.stderr);
  return { ...result, verdict };
}

// This previously required generated per-input proofs, including the large AST gate.
const payload = "x".repeat(220_000);
writeFileSync(path.join(root, "provider.mjs"), "export {};\n");
writeFileSync(
  source,
  `import "./provider.mjs";\nlet count = 0;\nconst hostType = typeof window;\nconst payload = ${JSON.stringify(payload)};\nexport { count, hostType, payload };\n`
);
writeFileSync(spec, JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#1", "#2", "#3", "#4"] } }));
writeFileSync(host, JSON.stringify({ profile: "browser-page" }));
mkdirSync(out);
const sentinel = path.join(out, "sentinel.bin");
const original = Buffer.from([0, 19, 127, 255]);
writeFileSync(sentinel, original);
const denied = run(source, spec, out, ["--require", "normal-completion", "--host-contract", host]);
assert.equal(denied.verdict.status, "rejected", denied.stdout + denied.stderr);
assert.equal(denied.verdict.reason_code, "no_compiled_rule", denied.stdout + denied.stderr);
assert.deepEqual(readdirSync(out), ["sentinel.bin"]);
assert.deepEqual(readFileSync(sentinel), original);
assert.doesNotMatch(denied.stderr, /"phase":"(?:execution_proof|ast_correspondence)"/);

// Explicit structural certification stays compiled and still checks the printed output.
const structuralSource = path.join(root, "structural.mjs");
const structuralSpec = path.join(root, "structural-spec.json");
const structuralOut = path.join(root, "structural-out");
writeFileSync(structuralSource, "const a = 1; export const result = a;\n");
writeFileSync(
  structuralSpec,
  JSON.stringify({
    entry: "entry.mjs",
    modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1"] },
    order: ["worker.mjs"],
    sequential: true,
  })
);
const flags = ["--structural", "--require", "all-outcomes"];
const structural = run(structuralSource, structuralSpec, structuralOut, flags);
assert.equal(structural.verdict.status, "certified", structural.stdout + structural.stderr);
assert.match(structural.verdict.scope, /^linked-structural/);
const checked = run(structuralSource, structuralSpec, structuralOut, [...flags, "--check-output"]);
assert.equal(checked.verdict.status, "certified", checked.stdout + checked.stderr);
const entry = path.join(structuralOut, "entry.mjs");
const entryText = readFileSync(entry, "utf8");
const tampered = entryText.replace("result = a", "result = 2");
assert.notEqual(tampered, entryText);
writeFileSync(entry, tampered);
const rejected = run(structuralSource, structuralSpec, structuralOut, [...flags, "--check-output"]);
assert.equal(rejected.verdict.status, "rejected", rejected.stdout + rejected.stderr);
assert.equal(readFileSync(entry, "utf8"), tampered, "readback refusal changed existing output");
assert(!existsSync(leanMarker), "split or check-output invoked lean from PATH");
