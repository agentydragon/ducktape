// A seeded random AST corpus must expose erasing the checker's write footprints.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const fuzz = fileURLToPath(new URL("./fuzz.mjs", import.meta.url));

function run(seed, binary, mutant) {
  assert(binary, "missing checker binary");
  const out = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? os.tmpdir(), "ast_write_mutation_"));
  try {
    const args = [
      fuzz,
      "--ast-corpus",
      "--count",
      "8",
      "--trees",
      "4",
      "--seed",
      String(seed),
      "--jobs",
      "2",
      "--require-accepted",
      "--out",
      out,
    ];
    const r = spawnSync(process.execPath, args, {
      encoding: "utf8",
      env: { ...process.env, TRANSLATE: path.resolve(binary) },
    });
    if (r.error) throw r.error;
    const output = r.stdout + r.stderr;
    assert.equal(r.signal, null, output);
    const summary = r.stdout.match(/^AST corpus: (\{.*\})$/m);
    const wrongLine = r.stdout.match(/^WRONG ACCEPTS: (\d+)$/m);
    assert(summary && wrongLine, `incomplete fuzzer run: ${output}`);
    const stats = JSON.parse(summary[1]);
    const wrong = Number(wrongLine[1]);
    assert(stats.writeGuidedDifferentPlans > 0, `seed ${seed}: no generated write/read move changed Node behavior`);
    assert(stats.nontrivialAcceptedPlans > 0, `seed ${seed}: no nontrivial accepted equivalent plan`);
    assert.equal(r.status, mutant ? 1 : 0, output);
    if (!mutant) {
      assert.equal(wrong, 0, output);
      assert(stats.writeGuidedObligations > 0, `seed ${seed}: no differing write-guided plan was refused by OrderOk`);
    } else {
      assert(wrong > 0, `seed ${seed}: write-erasure mutant survived`);
      assert.match(r.stderr, new RegExp(`${wrong} wrong accepts`), `unrelated failure: ${output}`);
      const records = readdirSync(out).filter((name) => name.startsWith("wrong_accept_"));
      assert.equal(records.length, wrong, `missing wrong-accept records: ${output}`);
      let verified = 0;
      for (const name of records) {
        const dir = path.join(out, name);
        const record = JSON.parse(readFileSync(path.join(dir, "record.json"), "utf8"));
        if (record.plan.strategy !== "astWriteGuided") continue;
        const witness = record.writeGuided;
        const ast = JSON.parse(readFileSync(path.join(dir, "ast.json"), "utf8"));
        const cert = JSON.parse(readFileSync(path.join(dir, "cert.json"), "utf8"));
        const writer = ast.body[witness.writer].expression;
        const reader = ast.body[witness.reader].declarations[0];
        assert.equal(writer.type, "UpdateExpression");
        assert.equal(writer.argument.name, witness.name);
        assert.equal(reader.id.name, witness.snapshot);
        assert.equal(reader.init.name, witness.name);
        assert(witness.writer < witness.reader, "source does not write before reading");
        const outputIndex = (i) => cert.pairs.find(([, , original]) => original === i);
        const emittedWriter = outputIndex(witness.writer);
        const emittedReader = outputIndex(witness.reader);
        assert(
          emittedWriter &&
            emittedReader &&
            emittedWriter[0] === emittedReader[0] &&
            emittedReader[1] < emittedWriter[1],
          "emitted plan did not move the read before the write"
        );
        assert.equal(record.verdict, "accept");
        assert.equal(record.same, false);
        assert.match(record.checker, /PLAN SOUND/);
        assert.equal(record.reference.status, 0);
        assert.equal(record.observed.status, 0);
        const snapshot = (stdout) => stdout.split("\n").find((line) => line.startsWith("write snapshot "));
        assert(snapshot(record.reference.stdout) && snapshot(record.observed.stdout), "missing snapshot output");
        assert.notEqual(snapshot(record.reference.stdout), snapshot(record.observed.stdout), "snapshot did not change");
        verified++;
      }
      assert(verified > 0, `seed ${seed}: no accepted write-guided mismatch: ${output}`);
    }
    console.log(
      `seed ${seed}, ${mutant ? "write-erasure mutant" : "baseline"}: ${stats.writeGuidedDifferentPlans} differing write plans, ${stats.nontrivialAcceptedPlans} nontrivial accepted plans, ${wrong} wrong accepts`
    );
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
}

for (const seed of [17, 2026]) {
  run(seed, process.env.TRANSLATE, false);
  run(seed, process.env.NO_WRITES, true);
}
