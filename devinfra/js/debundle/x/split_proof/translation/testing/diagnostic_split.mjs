import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { writeDeployment } from "./deployment_manifest.mjs";
import { dump } from "../parser.mjs";
import { split } from "./split.mjs";

export const run = (binary, args) => spawnSync(binary, args, { encoding: "utf8" });

const certificateResults = new Map();
function certificateFor({ input, spec, dir }) {
  const key = `${path.resolve(input)}\n${path.resolve(spec)}\n${path.resolve(dir)}`;
  if (!certificateResults.has(key)) {
    certificateResults.set(key, split({ ast: input, spec, out: path.join(dir, "certified-output") }));
  }
  return certificateResults.get(key);
}

process.on("exit", () => {
  const results = [...certificateResults.values()];
  const counts = {
    plans: results.length,
    certified: results.filter((result) => result.status === "certified").length,
    refused: results.filter((result) => result.status === "refused").length,
  };
  process.stderr.write(`certified split trials: ${JSON.stringify(counts)}\n`);
});

// These fixtures assign one ordinal per statement. Factory and identity controls
// move the penultimate statement unless they specify a different placement.
export function prepareSideSplit(name, code, selectedSide) {
  const dir = path.join(process.env.TEST_TMPDIR, name);
  mkdirSync(dir, { recursive: true });
  const source = dir + ".mjs";
  const input = dir + "-ast.json";
  const spec = dir + "-spec.json";
  const program = dump(code);
  const side = selectedSide ?? [program.body.length - 2];
  const modules = {
    "entry.mjs": program.body.flatMap((_, i) => (side.includes(i) ? [] : [`#${i}`])),
    "side.mjs": side.map((i) => `#${i}`),
  };
  writeFileSync(source, code);
  writeFileSync(input, JSON.stringify(program));
  writeFileSync(spec, JSON.stringify({ entry: "entry.mjs", modules }));
  return {
    dir,
    source,
    input,
    spec,
    certifiedOut: path.join(dir, "certified-output"),
  };
}

export function lowerDiagnostic({ input, spec, dir }, ...flags) {
  const certification = certificateFor({ input, spec, dir });
  // Callers use this only to inspect the legacy model diagnostics. Comparison
  // output uses the certified helper and never falls back to a forced preview.
  const result = run(process.env.LOWERSPLIT, ["--diagnostic", writeDeployment(input, spec, dir), dir, ...flags]);
  assert.match(result.stdout, /model diagnostic: /, result.stdout + result.stderr);
  return Object.assign(result, { certification });
}
