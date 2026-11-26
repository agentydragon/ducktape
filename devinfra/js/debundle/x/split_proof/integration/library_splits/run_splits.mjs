// Many splits of one real bundle: what the Lean checker says, and what Node does with the split program.
//
//   node run_splits.mjs <bundle> <strategy> [--bundles DIR] [--step N] [--seeds N] [--jobs N] [--footprints f.jsonl] [--out results.jsonl]
//
//   bundle    lodash | acorn | semver | zod | acorn.min | zod.min   (../../../lowering/bundles/<bundle>.{json,mjs})
//   strategy  halves | cuts | cuts_entry_first | blocks | random
//             cuts:   every single cut point c = step, 2*step, … (two modules [0, c) and [c, n))
//             cuts_entry_first: the same cuts, but the first module is the entry
//             blocks: k = 2…8 contiguous blocks
//             random: k ∈ {2, 3, 5, 8} modules, `seeds` random assignments each
//
// The bundle's footprints are computed once (`lower`, or the file given with `--footprints`); each split is checked
// with `split_preview --force --footprints` (checks, writes the modules even when rejected), then
// `smoke.mjs` on the entry module. One JSON line per split:
//   { bundle, strategy, args, verdict: "accept"|"reject", failed: [check…], node: "same"|"different"|"error", detail }
// Soundness claim under test: no split that is accepted behaves differently.
import { spawn } from "node:child_process";
import { copyFile, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { topLevelStatements } from "../../translation/scope_metadata.mjs";
import { spec as makeSpec } from "./gen_spec.mjs";
import { writeDeployment } from "../../translation/testing/deployment_manifest.mjs";
import { smoke } from "./smoke.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const preview = path.resolve(process.env.SPLIT_PREVIEW ?? path.join(here, "../../lean/.lake/build/bin/split_preview"));
const lower = path.resolve(process.env.LOWER ?? path.join(here, "../../lean/.lake/build/bin/lower"));

const [bundle, strategy, ...rest] = process.argv.slice(2);
const option = (name, fallback) => {
  const at = rest.indexOf(`--${name}`);
  return at < 0 ? fallback : rest[at + 1];
};
const lowering = path.resolve(option("bundles", path.join(here, "../../lowering/bundles")));
const step = Number(option("step", 1));
const seeds = Number(option("seeds", 20));
const jobs = Number(option("jobs", 4));
const out = option("out", `${bundle}_${strategy}.jsonl`);
const family = bundle.replace(/\.min$/, "");

const run = (cmd, args) =>
  new Promise((resolve, reject) => {
    const child = spawn(cmd, args);
    let stdout = "";
    let stderr = "";
    child.on("error", reject);
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
const n = topLevelStatements(bundleJson).length;
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

const lines = [];
const tally = {};
let next = 0;
async function worker() {
  while (next < plans.length) {
    const plan = plans[next++];
    const dir = await mkdtemp(path.join(tmpdir(), "split_"));
    try {
      const spec = makeSpec(n, plan.strategy, plan.args);
      await writeFile(path.join(dir, "spec.json"), JSON.stringify(spec));
      const checked = await run(preview, [
        writeDeployment(path.join(lowering, `${bundle}.json`), path.join(dir, "spec.json"), path.join(dir, "out")),
        path.join(dir, "out"),
        "--footprints",
        footprints,
        "--plan",
        path.join(dir, "plan.json"),
        "--force",
      ]);
      const failed = [...checked.stdout.matchAll(/^\s+FAIL\s+(.*)$/gm)].map(
        ([, what]) =>
          what.match(/^statement \d+ is an `(Import|Export)/)?.[1].concat("OutsideEntry") ?? what.split(/\s+/)[0]
      );
      if (checked.code !== 0 && checked.code !== 1)
        throw new Error(`split_preview exit ${checked.code}: ${checked.stderr.trim()}`);
      const verdict = checked.code === 0 ? "accept" : "reject";
      const got = await smoke(family, path.join(dir, "out", spec.entry));
      const node = JSON.stringify(got) === baseline ? "same" : "different";
      const detail = node === "same" ? "" : got.ok ? "returned a different value" : got.error;
      const emitted = JSON.parse(await readFile(path.join(dir, "plan.json"), "utf8"));
      const reordered = emitted.order.flatMap((m) => emitted.modules[m]).some((unit, i) => unit !== i);
      if (verdict === "accept" && node !== "same") {
        await copyFile(path.join(dir, "spec.json"), `${out}.${plan.strategy}-${plan.args.join("-")}.spec.json`);
      }
      const line = {
        bundle,
        strategy: plan.label ?? plan.strategy,
        args: plan.args,
        modules: Object.keys(spec.modules).length,
        verdict,
        reordered,
        failed,
        node,
        detail,
      };
      lines.push(line);
      const key = `${verdict}/${node}`;
      tally[key] = (tally[key] ?? 0) + 1;
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  }
}
await Promise.all(Array.from({ length: jobs }, worker));
// the workers finish in any order: one order for every run keeps a rerun's diff to what changed
const order = (a, b) =>
  a.args.length - b.args.length || a.args.reduce((d, x, i) => d || (x < b.args[i] ? -1 : x > b.args[i] ? 1 : 0), 0);
await writeFile(
  out,
  lines
    .sort(order)
    .map((l) => `${JSON.stringify(l)}\n`)
    .join("")
);
if (work) await rm(work, { recursive: true, force: true });
console.log(`${bundle} ${strategy}: ${plans.length} splits`, tally);
if (tally["accept/different"]) {
  console.error("UNSOUND: an accepted split behaves differently");
  process.exitCode = 1;
}

if (rest.includes("--require-accepted") && !tally["accept/same"]) throw new Error("no accepted matching split");
