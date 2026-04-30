// Black-box test helpers for the debundler. Zero dependencies on debundler
// internals — only node stdlib — so a re-implementation in another language
// can drive end-to-end tests against an external run_transform binary using
// the same harness.
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

export function createWebFixtureRoots(prefix) {
  const root = mkdtempSync(join(tmpdir(), prefix));
  return {
    extractedRoot: join(root, "extracted"),
    outRoot: join(root, "out"),
    snapshotRoot: join(root, "snapshot"),
  };
}

export function writeTextFile(path, content) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, content);
}

export function writeSnapshotFixture({ extractedRoot, files, jsFiles, snapshotRoot }) {
  // Mark the snapshot tree as ESM so node loads emitted .js files as modules.
  writeTextFile(join(snapshotRoot, "package.json"), `${JSON.stringify({ type: "module" }, null, 2)}\n`);
  for (const [relPath, content] of Object.entries(files)) {
    writeTextFile(join(snapshotRoot, relPath), content);
  }
  mkdirSync(extractedRoot, { recursive: true });
  writeTextFile(join(extractedRoot, "js-files.txt"), `${jsFiles.join("\n")}\n`);
}

export function runNodeScript(path) {
  const result = spawnSync(process.execPath, [path], { encoding: "utf8" });
  return {
    signal: result.signal,
    status: result.status,
    stderr: result.stderr,
    stdout: result.stdout,
  };
}
