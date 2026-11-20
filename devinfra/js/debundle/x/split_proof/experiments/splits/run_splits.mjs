// Many splits of one real bundle: what the Lean checker says, and what Node does with the split program.
//
//   node run_splits.mjs <bundle> <strategy> [--step N] [--seeds N] [--jobs N] [--footprints f.jsonl] [--out results.jsonl]
//
//   bundle    lodash | acorn | semver | zod | acorn.min | zod.min   (experiments/lowering/bundles/<bundle>.{json,mjs})
//   strategy  halves | cuts | cuts_entry_first | blocks | random
//             cuts:   every single cut point c = step, 2*step, … (two modules [0, c) and [c, n))
//             cuts_entry_first: the same cuts, but the first module is the entry
//             blocks: k = 2…8 contiguous blocks
//             random: k ∈ {2, 3, 5, 8} modules, `seeds` random assignments each
//
// The bundle's footprints are computed once (`lower`, or the file given with `--footprints`); each split is checked
// with `lowersplit --force --footprints` (checks, writes the modules even when rejected), then
// `smoke.mjs` on the entry module. One JSON line per split:
//   { bundle, strategy, args, verdict: "accept"|"reject", failed: [check…], node: "same"|"different"|"error", detail }
// Soundness claim under test: no split that is accepted behaves differently.
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile, appendFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spec as makeSpec } from "./gen_spec.mjs";
import { smoke } from "./smoke.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const lowering = path.join(here, "../lowering/bundles");
const lowersplit = path.join(here, "../../lean/.lake/build/bin/lowersplit");
const lower = path.join(here, "../../lean/.lake/build/bin/lower");

const [bundle, strategy, ...rest] = process.argv.slice(2);
const option = (name, fallback) => {
  const at = rest.indexOf(`--${name}`);
  return at < 0 ? fallback : rest[at + 1];
};
const step = Number(option("step", 1));
const seeds = Number(option("seeds", 20));
const jobs = Number(option("jobs", 4));
const out = option("out", `${bundle}_${strategy}.jsonl`);
const family = bundle.replace(/\.min$/, "");

const run = (cmd, args) =>
  new Promise((resolve) => {
    const child = spawn(cmd, args);
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    child.on("close", (code) => resolve({ code, stdout, stderr }));
  });

// The analysis takes minutes on a big bundle and does not depend on the split: run it once.
let work;
async function analyze() {
  const analysis = await run(lower, [path.join(lowering, `${bundle}.json`)]);
  if (analysis.code !== 0) throw new Error(`lower exit ${analysis.code}: ${analysis.stderr.split("\n").pop()}`);
  work = await mkdtemp(path.join(tmpdir(), "footprints_"));
  const file = path.join(work, "footprints.jsonl");
  await writeFile(file, analysis.stdout);
  return file;
}
const footprints = option("footprints", null) ?? (await analyze());
const bundleJson = JSON.parse(await readFile(path.join(lowering, `${bundle}.json`), "utf8"));
const n = bundleJson.statements.length;
const baseline = JSON.stringify(await smoke(family, path.join(lowering, `${bundle}.mjs`)));
if (!JSON.parse(baseline).ok) throw new Error(`the bundle itself fails its smoke test: ${baseline}`);

const plans = [];
if (strategy === "halves") plans.push({ strategy, args: [] });
else if (strategy === "cuts") for (let c = step; c < n; c += step) plans.push({ strategy: "cut", args: [c] });
else if (strategy === "cuts_entry_first")
  for (let c = step; c < n; c += step)
    plans.push({ strategy: "cut", args: [c, "entry-first"], label: "cut_entry_first" });
else if (strategy === "blocks") for (let k = 2; k <= 8; k++) plans.push({ strategy, args: [k] });
else if (strategy === "random")
  for (const k of [2, 3, 5, 8]) for (let seed = 1; seed <= seeds; seed++) plans.push({ strategy, args: [k, seed] });
else throw new Error(`unknown strategy ${strategy}`);

await writeFile(out, "");
const tally = {};
let next = 0;
async function worker() {
  while (next < plans.length) {
    const plan = plans[next++];
    const dir = await mkdtemp(path.join(tmpdir(), "split_"));
    try {
      const spec = makeSpec(n, plan.strategy, plan.args);
      await writeFile(path.join(dir, "spec.json"), JSON.stringify(spec));
      const checked = await run(lowersplit, [
        path.join(lowering, `${bundle}.json`),
        path.join(dir, "spec.json"),
        path.join(dir, "out"),
        "--footprints",
        footprints,
        "--force",
      ]);
      const failed = [...checked.stdout.matchAll(/^\s+FAIL\s+(.*)$/gm)].map(([, what]) =>
        /^statement \d+ is an/.test(what) ? "ExportOutsideEntry" : what.split(/\s+/)[0]
      );
      const verdict = checked.code === 0 ? "accept" : "reject";
      let node = "error";
      let detail = "";
      if (checked.code === 0 || checked.code === 1) {
        const got = await smoke(family, path.join(dir, "out", spec.entry));
        node = JSON.stringify(got) === baseline ? "same" : "different";
        if (node === "different") detail = got.ok ? "returned a different value" : got.error;
      } else detail = `lowersplit exit ${checked.code}: ${checked.stderr.trim().split("\n").pop()}`;
      const line = {
        bundle,
        strategy: plan.label ?? plan.strategy,
        args: plan.args,
        modules: Object.keys(spec.modules).length,
        verdict,
        failed,
        node,
        detail,
      };
      await appendFile(out, `${JSON.stringify(line)}\n`);
      const key = `${verdict}/${node}`;
      tally[key] = (tally[key] ?? 0) + 1;
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  }
}
await Promise.all(Array.from({ length: jobs }, worker));
if (work) await rm(work, { recursive: true, force: true });
console.log(`${bundle} ${strategy}: ${plans.length} splits`, tally);
if (tally["accept/different"]) {
  console.error("UNSOUND: an accepted split behaves differently");
  process.exitCode = 1;
}
