import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { writeDeployment } from "./deployment_manifest.mjs";
import { dump } from "../parser.mjs";

export const run = (binary, args) => spawnSync(binary, args, { encoding: "utf8" });

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
  return { dir, source, input, spec };
}

export function lowerDiagnostic({ input, spec, dir }, ...flags) {
  return run(process.env.LOWERSPLIT, ["--diagnostic", writeDeployment(input, spec, dir), dir, ...flags]);
}
