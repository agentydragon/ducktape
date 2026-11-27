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
assert.equal(denied.verdict.reason_code, "slice_profile", denied.stdout + denied.stderr);
assert.deepEqual(readdirSync(out), ["sentinel.bin"]);
assert.deepEqual(readFileSync(sentinel), original);

// Check an existing output tree as well as a request to publish one. The
// unchanged single-file placement lets the input be used as the disk fixture.
const readbackOut = path.join(root, "generated-only-readback");
mkdirSync(readbackOut);
writeFileSync(path.join(readbackOut, "entry.mjs"), readFileSync(source));
writeFileSync(path.join(readbackOut, "provider.mjs"), readFileSync(path.join(root, "provider.mjs")));
const readbackFiles = readdirSync(readbackOut);
const beforeReadback = readbackFiles.map((file) => readFileSync(path.join(readbackOut, file)));
const deniedReadback = run(source, spec, readbackOut, [
  "--require",
  "normal-completion",
  "--host-contract",
  host,
  "--check-output",
]);
assert.equal(deniedReadback.verdict.status, "rejected", deniedReadback.stdout + deniedReadback.stderr);
assert.equal(deniedReadback.verdict.reason_code, "slice_profile", deniedReadback.stdout + deniedReadback.stderr);
assert.deepEqual(readdirSync(readbackOut), readbackFiles);
assert.deepEqual(
  readbackFiles.map((file) => readFileSync(path.join(readbackOut, file))),
  beforeReadback
);
assert(!existsSync(leanMarker), "split or check-output invoked lean from PATH");
