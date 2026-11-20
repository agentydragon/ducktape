// The random AST corpus must expose omission of the observed-initializer fence.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const seed = Number(process.argv[2]);
const count = seed === 17 ? 15 : 28;
assert([17, 2026].includes(seed));
const fuzz = fileURLToPath(new URL("./fuzz.mjs", import.meta.url));
for (const [label, binary] of [
  ["baseline", process.env.TRANSLATE],
  ["mutant", process.env.NO_OBSERVED],
]) {
  assert(binary);
  const out = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? os.tmpdir(), "observed_fence_"));
  try {
    const r = spawnSync(
      process.execPath,
      [
        fuzz,
        "--ast-corpus",
        "--count",
        String(count),
        "--trees",
        "2",
        "--seed",
        String(seed),
        "--jobs",
        "4",
        "--require-accepted",
        "--out",
        out,
      ],
      { encoding: "utf8", env: { ...process.env, TRANSLATE: path.resolve(binary) } }
    );
    if (r.error) throw r.error;
    const output = r.stdout + r.stderr;
    const summary = r.stdout.match(/^AST corpus: (\{.*\})$/m);
    const wrong = r.stdout.match(/^WRONG ACCEPTS: (\d+)$/m);
    assert(summary && wrong && !r.signal, output);
    const stats = JSON.parse(summary[1]);
    const numberWrong = Number(wrong[1]);
    assert(stats.guidedDifferentPlans > 0, `seed ${seed}: no generated callback changed Node behavior`);
    if (label === "baseline") {
      assert.equal(r.status, 0, output);
      assert.equal(numberWrong, 0, output);
      assert(stats.indirectGuidedObligations > 0, `seed ${seed}: no indirect divergent case reached OrderOk`);
    } else {
      assert.equal(r.status, 1, output);
      assert(numberWrong > 0, output);
      assert.match(r.stderr, new RegExp(`${numberWrong} wrong accepts`), output);
      const records = readdirSync(out).filter((n) => n.startsWith("wrong_accept_"));
      assert.equal(records.length, numberWrong);
      let verified = 0;
      for (const name of records) {
        const dir = path.join(out, name);
        const read = (file) => JSON.parse(readFileSync(path.join(dir, file), "utf8"));
        const record = read("record.json");
        if (record.plan.strategy !== "astGuided" || record.guidedInitializer !== "Literal") continue;
        if (seed === 2026 && record.guidedCallbackShape !== "object") continue;
        const { writer, reader } = record.guidedMove;
        const ast = read("ast.json");
        const declaration = ast.body[writer];
        assert.equal(declaration.type, "VariableDeclaration");
        assert.equal(declaration.declarations[0].init.type, "Literal");
        const callback = ast.body[reader];
        assert.equal(callback.type, "ExpressionStatement");
        assert.equal(callback.expression.arguments[0].value, "callback");
        const pairs = read("cert.json").pairs;
        const outputIndex = (i) => pairs.find(([, , original]) => original === i);
        const emittedWriter = outputIndex(writer);
        const emittedReader = outputIndex(reader);
        assert(
          writer < reader &&
            emittedWriter &&
            emittedReader &&
            emittedWriter[0] === emittedReader[0] &&
            emittedReader[1] < emittedWriter[1]
        );
        assert.equal(record.verdict, "accept");
        assert.match(record.checker, /PLAN SOUND/);
        assert.equal(record.reference.status, 0);
        assert.match(record.reference.stdout, /callback /);
        assert.equal(record.observed.error, "ReferenceError");
        assert.notEqual(record.observed.status, 0);
        verified++;
      }
      assert(verified > 0, `seed ${seed}: no verified accepted literal callback crossing: ${output}`);
    }
    console.log(
      `seed ${seed} ${label}: ${stats.guidedDifferentPlans} divergent callback plans, ${stats.indirectGuidedObligations} indirect obligations, ${numberWrong} wrong accepts`
    );
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
}
