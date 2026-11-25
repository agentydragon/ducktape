// Throw a generated Lean file at Lean with a ladder of strategies; report what each one did.
//
//   node prove.mjs <Generated.lean> [--ladder "decide +kernel;native_decide"] [--timeout 120]
//
// The file contains `theorem plan_valid : Valid bundle spec plan := by decide +kernel` (what `lowersplit --generate-instance` writes).
// For each strategy the tactic after `by` is replaced, the file is run with `lake env lean --json`, and the first
// strategy that Lean accepts wins. A strategy that runs past the timeout is killed and counts as a failure.
//
// Nothing here searches for a proof term: the theorem is decided by computation (`Valid` is decidable) and
// `split_correct` turns the decision into `Correct`. A strategy only says how to run that computation:
//   decide +kernel   the kernel evaluates it            trusts: the kernel
//   native_decide    the compiled code evaluates it     trusts: the kernel, Lean's compiler and runtime
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const leanProject = path.join(here, "../../lean");
const [file, ...rest] = process.argv.slice(2);
const option = (name, fallback) => {
  const at = rest.indexOf(`--${name}`);
  return at < 0 ? fallback : rest[at + 1];
};
const ladder = option("ladder", "decide +kernel;native_decide").split(";");
const timeoutMs = Number(option("timeout", 120)) * 1000;

const source = await readFile(file, "utf8");
const goal = /(theorem plan_valid : Valid bundle spec plan := by )[^\n]*/;
if (!goal.test(source)) throw new Error("no `theorem plan_valid … := by …` line in the file");

async function attempt(strategy, dir) {
  const attemptFile = path.join(dir, "Attempt.lean");
  await writeFile(attemptFile, source.replace(goal, `$1${strategy}`));
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const child = spawn("lake", ["env", "lean", "--json", attemptFile], { cwd: leanProject });
    let out = "";
    const timer = setTimeout(() => child.kill("SIGKILL"), timeoutMs);
    child.on("error", reject);
    child.stdout.on("data", (d) => (out += d));
    child.on("close", (code, signal) => {
      clearTimeout(timer);
      const seconds = (Date.now() - started) / 1000;
      const messages = out
        .split("\n")
        .filter(Boolean)
        .map((l) => JSON.parse(l));
      const errors = messages.filter((m) => m.severity === "error");
      const axioms = messages.map((m) => m.data).find((d) => d?.includes("depends on axioms"));
      if (signal) resolve({ strategy, outcome: `timeout after ${timeoutMs / 1000} s`, seconds });
      else if (code === 0 && !errors.length)
        resolve({ strategy, outcome: "proved", seconds, axioms: axioms?.replace(/^.*axioms: /, "") });
      else resolve({ strategy, outcome: `failed: ${(errors[0]?.data ?? "").split("\n")[0].slice(0, 110)}`, seconds });
    });
  });
}

const dir = await mkdtemp(path.join(tmpdir(), "prove_"));
try {
  for (const strategy of ladder) {
    const r = await attempt(strategy, dir);
    console.log(
      `${r.strategy.padEnd(18)} ${r.outcome.padEnd(48)} ${r.seconds.toFixed(1).padStart(6)} s  ${r.axioms ?? ""}`
    );
    if (r.outcome === "proved") process.exit(0);
  }
  console.log("no strategy proved it");
  process.exit(1);
} finally {
  await rm(dir, { recursive: true, force: true });
}
