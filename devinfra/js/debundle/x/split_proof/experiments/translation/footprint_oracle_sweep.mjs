// Run the footprint oracle over the programs of the generators of `coverage_audit.mjs` (and over files).
//
//   node footprint_oracle_sweep.mjs [--only id,id] [--samples N] [--seed S] [--runs R] [--timeout MS] [--keep DIR] [--jobs J] [--files a.mjs,b.mjs]
//
// Prints per generator how many programs were valid inputs (they complete, are deterministic and patch nothing), how many
// (unit, binding) changes the oracle saw, and the findings: `unwritten` is a wrong footprint, `unlisted` a gap in the
// `writes` of an effect unit, `global`, `printed` and `foreign-definition` as in `footprint_oracle.mjs`. `--keep DIR`
// writes every program with a finding there, for reduction. Exits 1 if any program has a finding other than `unlisted`.
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { GENERATORS } from "./coverage_audit.mjs";
import { checkProgram } from "./footprint_oracle.mjs";

// The fuzzers' generated bundles satisfy premise P3 (the world adds no accessors or hooks); Shift's programs are run with
// no premise beyond the always-on ones, as `shift_fuzz_test` does.
const P3 = new Set(["templates", "callback", "ast"]);

/** Check `programs` (`{ name, source }`), `jobs` at a time; the tally of what the oracle saw. */
export async function sweep(label, programs, { premises, runs, jobs, keep, timeoutMs }) {
  const tally = {
    label,
    programs: programs.length,
    valid: 0,
    invalid: {},
    units: 0,
    changed: 0,
    findings: { unwritten: 0, unlisted: 0, global: 0, printed: 0, "foreign-definition": 0 },
    withFindings: [],
    tail: 0,
    patchers: 0,
  };
  let next = 0;
  await Promise.all(
    Array.from({ length: jobs }, async () => {
      while (next < programs.length) {
        const program = programs[next++];
        const result = await checkProgram(program.source, { premises, runs, timeoutMs });
        if (result.invalid) {
          const reason = result.invalid
            .replace(/\(exit.*$/, "")
            .replace(/:.*$/, "")
            .slice(0, 80);
          tally.invalid[reason] = (tally.invalid[reason] ?? 0) + 1;
          continue;
        }
        tally.valid++;
        tally.units += result.units;
        tally.changed += result.changed;
        tally.tail += result.tail.length;
        if (result.patched.length > 0) tally.patchers++;
        for (const f of result.findings) tally.findings[f.kind]++;
        if (result.findings.some((f) => f.kind !== "unlisted") || result.findings.length > 0) {
          tally.withFindings.push({ name: program.name, findings: result.findings });
          if (keep) {
            mkdirSync(keep, { recursive: true });
            writeFileSync(path.join(keep, `${label}_${program.name}.mjs`), program.source);
            writeFileSync(
              path.join(keep, `${label}_${program.name}.findings.json`),
              JSON.stringify(result.findings, null, 2)
            );
          }
        }
      }
    })
  );
  return tally;
}

export function print(tally) {
  const f = tally.findings;
  console.log(
    `${tally.label}: ${tally.valid}/${tally.programs} valid inputs, ${tally.units} units, ${tally.changed} changes seen; ` +
      `unwritten ${f.unwritten}, unlisted ${f.unlisted}, global ${f.global}, printed ${f.printed}, foreign definitions ${f["foreign-definition"]}; ` +
      `${tally.tail} bindings changed after the last unit; ${tally.patchers} programs patch a standard object (premise P1)`
  );
  for (const [reason, n] of Object.entries(tally.invalid)) console.log(`    ${n} not valid: ${reason}`);
  for (const w of tally.withFindings.slice(0, 20))
    for (const finding of w.findings.slice(0, 3))
      console.log(
        `    ${w.name} unit ${finding.unit} ${finding.kind} ${finding.name}: ${finding.source.replace(/\s+/g, " ").slice(0, 140)}`
      );
}

async function main() {
  const args = process.argv.slice(2);
  const option = (name, fallback) => (args.includes(`--${name}`) ? args[args.indexOf(`--${name}`) + 1] : fallback);
  const samples = Number(option("samples", 50));
  const seed = Number(option("seed", 1));
  const runs = Number(option("runs", 1));
  const jobs = Number(option("jobs", 2));
  const timeoutMs = Number(option("timeout", 10000));
  const keep = option("keep", undefined);
  const only = option("only", undefined)?.split(",");
  const files = option("files", undefined)?.split(",");
  const work = mkdtempSync(path.join(process.env.TMPDIR ?? os.tmpdir(), "footprint_sweep_"));
  let failed = false;
  try {
    if (files) {
      const tally = await sweep(
        "files",
        files.map((file) => ({ name: path.basename(file), source: readFileSync(file, "utf8") })),
        {
          premises: undefined,
          runs,
          jobs,
          keep,
          timeoutMs,
        }
      );
      print(tally);
      failed ||= Object.entries(tally.findings).some(([kind, n]) => kind !== "unlisted" && n > 0);
    }
    for (const gen of files ? [] : GENERATORS.filter((g) => !only || only.includes(g.id))) {
      const dir = path.join(work, gen.id);
      mkdirSync(dir, { recursive: true });
      const produced = await gen.produce({ count: Math.min(samples, gen.maxCount ?? samples), seed, work: dir });
      const programs = produced
        .map((p, i) => ({ name: `s${seed}_${i}`, source: (p.prelude ?? "") + p.source, skipped: p.skipped }))
        .filter((p) => !p.skipped);
      const tally = await sweep(gen.id, programs, {
        premises: P3.has(gen.id) ? { noWorldHooks: true } : {},
        runs,
        jobs,
        keep,
        timeoutMs,
      });
      print(tally);
      failed ||= Object.entries(tally.findings).some(([kind, n]) => kind !== "unlisted" && n > 0);
    }
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
  process.exit(failed ? 1 : 0);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await main();
