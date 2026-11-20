// Verify that the generated callback corpus catches each real guard removal.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const fuzz = fileURLToPath(new URL("./fuzz.mjs", import.meta.url));

function run(name, binary, seed, expectedWrongRounds) {
  assert(binary, `missing ${name} checker path`);
  const artifacts = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? os.tmpdir(), "callback_mutation_"));
  try {
    const args = [
      fuzz,
      "--callback-corpus",
      "--count",
      "16",
      "--trees",
      "2",
      "--seed",
      String(seed),
      "--jobs",
      "2",
      "--require-accepted",
      "--out",
      artifacts,
    ];
    const child = spawnSync(process.execPath, args, {
      encoding: "utf8",
      env: { ...process.env, TRANSLATE: path.resolve(binary) },
    });
    if (child.error) throw child.error;
    const output = child.stdout + child.stderr;
    assert.equal(child.signal, null, `${name} was killed: ${output}`);
    const match = child.stdout.match(/^WRONG ACCEPTS: (\d+)$/m);
    assert(match, `${name} did not finish the corpus: ${output}`);
    const wrong = Number(match[1]);
    assert.equal(child.status, expectedWrongRounds.length ? 1 : 0, `${name} exited unexpectedly: ${output}`);
    if (expectedWrongRounds.length === 0) assert.equal(wrong, 0, `${name} wrong accepts: ${output}`);
    else {
      assert(wrong > 0, `${name} survived: ${output}`);
      for (const round of expectedWrongRounds) {
        const file = path.join(artifacts, `wrong_accept_${round}_1`, "record.json");
        assert(existsSync(file), `${name}: expected a wrong accept for round ${round}; ${output}`);
        const record = JSON.parse(readFileSync(file, "utf8"));
        assert.equal(record.verdict, "accept", `${name}, round ${round}: checker did not accept`);
        assert.equal(record.same, false, `${name}, round ${round}: behavior matched`);
        assert.equal(record.reference.status, 0, `${name}, round ${round}: bundle failed`);
        assert.equal(record.observed.error, "ReferenceError", `${name}, round ${round}: unexpected failure`);
      }
      assert.match(
        child.stderr,
        new RegExp(`${wrong} wrong accepts`),
        `${name} failed for an unrelated reason: ${output}`
      );
    }
    const corpus = child.stdout.match(/^callback corpus: (\{.*\})$/m);
    assert(corpus, `${name} did not report coverage: ${output}`);
    const coverage = JSON.parse(corpus[1]);
    for (const kind of ["identity", "factory"])
      for (const alias of ["direct", "alias", seed % 2 ? "object" : "array"])
        for (const slot of ["global", "slot"])
          for (const schedule of ["safe", "unsafe"])
            assert.equal(
              coverage[`${kind}/${alias}/${slot}/${schedule}`],
              alias === "direct" ? 2 : 1,
              `${name}: missing ${kind}/${alias}/${slot}/${schedule}`
            );
    console.log(
      `${name}, seed ${seed}: 16 accepted safe controls, 16 differing unsafe schedules, ${wrong} wrong accepts`
    );
  } finally {
    rmSync(artifacts, { recursive: true, force: true });
  }
}

// Every run demands all safe controls are accepted and Node-equivalent; the
// fuzzer also verifies every unsafe schedule differs in Node. The two seeds
// exercise both object and array aliases.
for (const seed of [1, 2026]) {
  run("baseline", process.env.TRANSLATE, seed, []);
  run("identity guard removed", process.env.IDENTITY_MUTANT, seed, [0, 4]);
  run("factory guard removed", process.env.FACTORY_MUTANT, seed, [2, 6]);
  run("deferred reads removed", process.env.NO_DEFERRED_MUTANT, seed, [0, 2]);
  // The inert-initializer guard independently protects these aliases.
  run("alias reads removed, inert guard retained", process.env.NO_ALIAS_MUTANT, seed, []);
  run("alias reads and inert guard removed", process.env.NO_ALIAS_AND_INERT_MUTANT, seed, [1, 3, 9, 11]);
}
