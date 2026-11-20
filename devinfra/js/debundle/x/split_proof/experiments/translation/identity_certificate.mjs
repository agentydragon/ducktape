// Generate the kernel-check fixture with the same raw-source frontend and
// LowerSplit executable used for the emitted modules.
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const code = readFileSync(path.join(here, "fixtures/local_identity/bundle.js"), "utf8");
const temp = mkdtempSync(path.join(tmpdir(), "identity-certificate-"));
try {
  const ast = path.join(temp, "ast.json");
  const spec = path.join(temp, "spec.json");
  const certificate = path.join(temp, "Instance.lean");
  writeFileSync(ast, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  writeFileSync(
    spec,
    JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["identity", "result", "#3"], "side.mjs": ["#2"] } })
  );
  const result = spawnSync(process.env.LOWERSPLIT, [ast, spec, path.join(temp, "out"), "--lean", certificate], {
    encoding: "utf8",
  });
  if (result.status !== 0) throw new Error(`LowerSplit failed: ${result.stdout}\n${result.stderr}`);
  if (!result.stdout.includes("inferred identity call")) throw new Error("identity was not inferred");
  process.stdout.write(readFileSync(certificate, "utf8"));
} finally {
  rmSync(temp, { recursive: true, force: true });
}
