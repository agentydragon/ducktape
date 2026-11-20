// Verify that the generated callback corpus catches each real guard removal.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const fuzz = fileURLToPath(new URL("./fuzz.mjs", import.meta.url));
const args = [
  fuzz,
  "--callback-corpus",
  "--count",
  "8",
  "--trees",
  "2",
  "--seed",
  "1",
  "--jobs",
  "2",
  "--require-accepted",
];

function run(name, binary, expectedWrongRounds) {
  assert(binary, `missing ${name} checker path`);
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
  if (expectedWrongRounds.length === 0) assert.equal(wrong, 0, `baseline wrong accepts: ${output}`);
  else {
    assert(wrong > 0, `${name} survived: ${output}`);
    for (const round of expectedWrongRounds)
      assert.match(
        child.stdout,
        new RegExp(`  bundle ${round} tree 1 \\(`),
        `${name} missed round ${round}: ${output}`
      );
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
    for (const alias of ["direct", "alias"])
      for (const slot of ["global", "slot"])
        for (const schedule of ["safe", "unsafe"])
          assert.equal(
            coverage[`${kind}/${alias}/${slot}/${schedule}`],
            1,
            `${name}: missing ${kind}/${alias}/${slot}/${schedule}`
          );
  console.log(`${name}: 8 accepted safe controls, 8 differing unsafe schedules, ${wrong} wrong accepts`);
}

// Each run demands all eight safe controls are accepted and Node-equivalent;
// the fuzzer also verifies every unsafe schedule differs in Node.
run("baseline", process.env.TRANSLATE, []);
run("identity guard removed", process.env.IDENTITY_MUTANT, [0, 1]);
run("factory guard removed", process.env.FACTORY_MUTANT, [2, 3]);
run("deferred reads removed", process.env.NO_DEFERRED_MUTANT, [0, 2]);
run("alias reads removed", process.env.NO_ALIAS_MUTANT, [1, 3]);
