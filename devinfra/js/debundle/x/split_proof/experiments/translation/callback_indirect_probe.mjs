// Diagnostic only: a surviving mutant is a measured gap, not a test failure.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
const fuzz = fileURLToPath(new URL("./fuzz.mjs", import.meta.url));
for (const seed of [17, 2026]) {
  for (const [kind, binary] of [
    ["baseline", process.env.TRANSLATE],
    ["fence mutant", process.env.NO_OBSERVED],
  ]) {
    assert(binary);
    const r = spawnSync(
      process.execPath,
      [
        fuzz,
        "--ast-corpus",
        "--count",
        "64",
        "--trees",
        "2",
        "--seed",
        String(seed),
        "--jobs",
        "4",
        "--require-accepted",
        ...(kind === "baseline" ? ["--require-indirect-guided"] : []),
      ],
      { encoding: "utf8", env: { ...process.env, TRANSLATE: path.resolve(binary) } }
    );
    if (r.error) throw r.error;
    const output = r.stdout + r.stderr;
    const summary = r.stdout.match(/^AST corpus: (\{.*\})$/m);
    const wrong = r.stdout.match(/^WRONG ACCEPTS: (\d+)$/m);
    assert(summary && wrong && !r.signal, output);
    const count = Number(wrong[1]);
    assert.equal(r.status, count ? 1 : 0, output);
    if (count) assert.match(r.stderr, new RegExp(`${count} wrong accepts`), output);
    if (kind === "baseline") assert.equal(count, 0, output);
    console.log(seed, kind, summary[1], `wrong accepts: ${count}`);
  }
}
