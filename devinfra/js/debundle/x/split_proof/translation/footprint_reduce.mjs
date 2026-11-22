// Reduce a program that the footprint oracle finds a wrong footprint in to the units that matter.
//
//   node footprint_reduce.mjs <program.mjs> [--unit "text of the unit"] [--kind unwritten] [--premises p.json] [--out reduced.mjs]
//
// The program is read one unit per line (what `gen/` writes). A candidate is the program without some of the lines; it
// keeps the finding if it is still a valid input (it completes and is deterministic) and the unit with the same text is
// still reported with the same kind and name. A `crash` of `lower` belongs to no unit: any crash keeps it. `reduceLines` is
// delta debugging over the lines. Only whole lines are removed: shrinking what is inside a unit is left to a person.
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { checkProgram } from "./footprint_oracle.mjs";

/**
 * The smallest list of `lines` (found greedily) for which `holds(lines)` is true, given that it is true for `lines`:
 * delta debugging with chunks of decreasing size. `keep` are indexes that stay.
 */
export async function reduceLines(lines, holds, keep = new Set()) {
  let current = lines.map((text, index) => ({ text, index }));
  let chunk = Math.max(1, Math.floor(current.length / 2));
  for (;;) {
    let removed = false;
    for (let start = 0; start < current.length;) {
      const candidate = current.filter((c, i) => i < start || i >= start + chunk || keep.has(c.index));
      if (candidate.length < current.length && (await holds(candidate.map((c) => c.text)))) {
        current = candidate;
        removed = true;
      } else start += chunk;
    }
    if (chunk === 1) {
      if (!removed) break;
    } else chunk = Math.max(1, Math.floor(chunk / 2));
  }
  return current.map((c) => c.text);
}

async function main() {
  const args = process.argv.slice(2);
  const option = (name, fallback) => (args.includes(`--${name}`) ? args[args.indexOf(`--${name}`) + 1] : fallback);
  const file = args.find(
    (a, i) => !a.startsWith("--") && !["--unit", "--kind", "--premises", "--out"].includes(args[i - 1])
  );
  if (!file)
    throw new Error(
      "usage: footprint_reduce.mjs <program.mjs> [--unit text] [--kind unwritten] [--premises p.json] [--out file]"
    );
  const premisesFile = option("premises", undefined);
  const premises = premisesFile ? JSON.parse(readFileSync(premisesFile, "utf8")) : { noWorldHooks: true };
  const kind = option("kind", "unwritten");
  const lines = readFileSync(file, "utf8")
    .split("\n")
    .filter((l) => l !== "");
  const first = await checkProgram(lines.join("\n") + "\n", { premises, runs: 1 });
  if (first.invalid) throw new Error(`not a valid input: ${first.invalid}`);
  const wanted = option("unit", undefined);
  const target = first.findings.find((f) => f.kind === kind && (!wanted || f.source.includes(wanted)));
  if (!target) throw new Error(`no ${kind} finding${wanted ? ` in a unit with ${JSON.stringify(wanted)}` : ""}`);
  const holds = async (candidate) => {
    const result = await checkProgram(candidate.join("\n") + "\n", { premises, runs: 1 });
    return (
      !result.invalid &&
      result.findings.some(
        (f) => f.kind === target.kind && (f.kind === "crash" || (f.name === target.name && f.source === target.source))
      )
    );
  };
  const keep = new Set(lines.flatMap((l, i) => (target.source !== "" && l === target.source ? [i] : [])));
  const reduced = await reduceLines(lines, holds, keep);
  const text = reduced.join("\n") + "\n";
  const out = option("out", undefined);
  if (out) writeFileSync(out, text);
  else process.stdout.write(text);
  console.error(
    `${lines.length} units reduced to ${reduced.length}: ${target.kind} ${target.name} in ${target.source.slice(0, 100)}`
  );
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await main();
