import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

if (process.env.ACORN_RLOCATION)
  process.env.JS_MODULE_PROOF_ACORN = resolve(process.env.TEST_SRCDIR, process.env.ACORN_RLOCATION);
process.env.JS_MODULE_PROOF_LEAN = resolve(process.env.TEST_SRCDIR, process.env.LEAN_RLOCATION);
const here = dirname(fileURLToPath(import.meta.url));
const root = await mkdtemp(join(process.env.TEST_TMPDIR, "combined-"));
const original = join(root, "original.mjs");
await writeFile(original, await readFile(join(here, "example/combined/original.mjs")));
function cli(...args) {
  const child = spawnSync(process.execPath, [join(here, "main.mjs"), ...args], { encoding: "utf8" });
  const text = child.stdout || child.stderr;
  return { status: child.status, value: JSON.parse(text) };
}
const models =
  (await readFile(join(here, "Semantics.lean"), "utf8")) +
  "\n" +
  (await readFile(join(here, "Effects.lean"), "utf8")) +
  "\n" +
  (await readFile(join(here, "Combined.lean"), "utf8"));
for (const safe of [true, false]) {
  const label = safe ? "safe" : "unsafe";
  const assignment = join(root, `${label}.json`);
  const out = join(root, label);
  await writeFile(assignment, await readFile(join(here, `example/combined/${label}.json`)));
  assert.equal(cli("generate", original, assignment, out).value.result, "generated");
  const checked = cli("validate", original, assignment, out);
  assert.equal(checked.value.result, safe ? "proved under assumptions" : "unsafe", JSON.stringify(checked));
  if (!safe) {
    assert.deepEqual(checked.value.original, { events: [1], current: 1 });
    assert.deepEqual(checked.value.output, { events: [0], current: 1 });
  }
  const proof = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + checked.value.certificate,
  });
  assert.equal(proof.status, 0, proof.stdout + proof.stderr);
  assert.match(
    proof.stdout,
    safe ? /'combined_calls' does not depend on any axioms/ : /'combined_difference' does not depend on any axioms/
  );
  const reader = join(out, "reader.mjs");
  await writeFile(reader, (await readFile(reader, "utf8")).replace("record(count)", "record(999)"));
  assert.equal(cli("validate", original, assignment, out).value.result, "unknown");
}
