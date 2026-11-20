// Reproducible broader random sample; unlike the selected mutation tests,
// individual seeds need not contain a witness.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const fuzz = fileURLToPath(new URL("./fuzz.mjs", import.meta.url));
const seeds = process.argv.length > 2 ? process.argv.slice(2).map(Number) : [1, 17, 42, 2026, 2027, 4001, 9001, 12345];
for (const seed of seeds) {
  assert(Number.isSafeInteger(seed) && seed >= 0);
  for (const [label, binary] of [
    ["baseline", process.env.TRANSLATE],
    ["mutant", process.env.NO_OBSERVED],
  ]) {
    assert(binary);
    const args = [
      fuzz,
      "--ast-corpus",
      "--count",
      "32",
      "--trees",
      "2",
      "--seed",
      String(seed),
      "--jobs",
      "4",
      "--require-accepted",
    ];
    if (process.env.TEST_UNDECLARED_OUTPUTS_DIR) {
      const out = path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, `${seed}_${label}`);
      mkdirSync(out, { recursive: true });
      args.push("--out", out);
    }
    const r = spawnSync(process.execPath, args, {
      encoding: "utf8",
      env: { ...process.env, TRANSLATE: path.resolve(binary) },
    });
    if (r.error) throw r.error;
    const output = r.stdout + r.stderr;
    const summary = r.stdout.match(/^AST corpus: (\{.*\})$/m);
    const wrong = r.stdout.match(/^WRONG ACCEPTS: (\d+)$/m);
    assert(summary && wrong && !r.signal, output);
    const count = Number(wrong[1]);
    assert.equal(r.status, count ? 1 : 0, output);
    if (count) assert.match(r.stderr, new RegExp(`${count} wrong accepts`), output);
    if (label === "baseline") assert.equal(count, 0, output);
    const stats = JSON.parse(summary[1]);
    console.log(
      `seed ${seed} ${label}: ${stats.guidedDifferentPlans} divergent callback plans, ${stats.indirectGuidedObligations} indirect obligations, ${count} wrong accepts`
    );
  }
}
