import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

if (process.env.ACORN_RLOCATION) {
  process.env.JS_MODULE_PROOF_ACORN = resolve(process.env.TEST_SRCDIR, process.env.ACORN_RLOCATION);
}
const { validate } = await import("./checker.mjs");

const here = dirname(fileURLToPath(import.meta.url));
const example = join(here, "example");
const original = join(example, "original.mjs");
const assignment = join(example, "assignment.json");
const dir = await mkdtemp(join(process.env.TEST_TMPDIR, "split-"));
process.env.JS_MODULE_PROOF_LEAN = resolve(process.env.TEST_SRCDIR, process.env.LEAN_RLOCATION);

const cli = (...args) => spawnSync(process.execPath, [join(here, "main.mjs"), ...args], { encoding: "utf8" });
const generated = cli("generate", original, assignment, dir);
assert.equal(generated.status, 0, generated.stderr);
assert.equal(JSON.parse(generated.stdout).result, "generated");
const accepted = cli("validate", original, assignment, dir);
assert.equal(accepted.status, 0, accepted.stderr || accepted.stdout);
const result = JSON.parse(accepted.stdout);
assert.equal(result.result, "proved under assumptions", JSON.stringify(result));
assert.match(result.certificate, /theorem extracted_counter_certificate/);
assert.match(result.certificate, /theorem shared_counter_semantics/);
assert.match(result.certificate, /theorem extracted_state_certificate/);
assert.match(result.certificate, /theorem source_startup_correct/);
assert.match(result.certificate, /theorem output_startup_correct/);
assert.match(result.certificate, /theorem output_import_linked/);
assert.match(result.certificate, /theorem extracted_operational_sequence/);
assert.match(result.certificate, /def extractedOutput : OutputAst/);
assert.match(result.certificate, /outputProgram extractedSource extractedOutput/);
assert.equal(result.outputAst.importedName, "count");
const shared = await readFile(join(here, "Semantics.lean"), "utf8");
const effects = await readFile(join(here, "Effects.lean"), "utf8");
const counter = await readFile(join(here, "Counter.lean"), "utf8");
const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
  encoding: "utf8",
  input: `${shared}\n${effects}\n${counter}\n${result.certificate}`,
});
assert.equal(replay.status, 0, replay.stderr || replay.stdout);
assert.match(replay.stdout, /'shared_counter_semantics' does not depend on any axioms/);
assert.match(replay.stdout, /'shared_counter_schedule' does not depend on any axioms/);

const liveReader = await readFile(join(dir, "reader.mjs"), "utf8");
await writeFile(
  join(dir, "reader.mjs"),
  liveReader
    .replace("export function", "const snapshot = count;\nexport function")
    .replace("return count;", "return snapshot;")
);
assert.deepEqual(await validate(original, assignment, dir), {
  result: "unsafe",
  calls: ["increment", "current"],
  original: 1,
  output: 0,
});
await writeFile(join(dir, "reader.mjs"), liveReader);

await writeFile(join(dir, "reader.mjs"), liveReader.replace("./state.mjs", "./missing.mjs"));
const missingImport = await validate(original, assignment, dir);
assert.equal(missingImport.result, "unknown");
assert.equal(missingImport.reason, "Lean could not resolve the extracted ASTs");
await writeFile(join(dir, "reader.mjs"), liveReader);

const unsupported = join(dir, "unsupported.mjs");
await writeFile(unsupported, `${await readFile(original, "utf8")}\ndebugger;\n`);
await assert.rejects(validate(unsupported, assignment, dir), /unknown:/);
const rejected = cli("validate", unsupported, assignment, dir);
assert.equal(rejected.status, 2);
assert.equal(JSON.parse(rejected.stderr).result, "unknown");

const unresolved = join(dir, "unresolved.mjs");
await writeFile(unresolved, (await readFile(original, "utf8")).replace("return count;", "return missing;"));
const unresolvedResult = await validate(unresolved, assignment, dir);
assert.equal(unresolvedResult.result, "unknown");
assert.equal(unresolvedResult.reason, "Lean could not resolve the extracted ASTs");

const lean = process.env.JS_MODULE_PROOF_LEAN;
process.env.JS_MODULE_PROOF_LEAN = join(dir, "missing-lean");
assert.equal((await validate(original, assignment, dir)).result, "unknown");
process.env.JS_MODULE_PROOF_LEAN = lean;

const renamedSource = join(dir, "renamed.mjs");
const renamedSpec = join(dir, "renamed.json");
const renamedDir = join(dir, "renamed-output");
await writeFile(
  renamedSource,
  (await readFile(original, "utf8"))
    .replaceAll("count", "total")
    .replaceAll("increment", "advance")
    .replaceAll("current", "read")
);
await writeFile(renamedSpec, JSON.stringify({ "store.mjs": ["total", "advance"], "view.mjs": ["read"] }));
const renamedGenerated = cli("generate", renamedSource, renamedSpec, renamedDir);
assert.equal(renamedGenerated.status, 0, renamedGenerated.stderr);
const renamedReaderPath = join(renamedDir, "view.mjs");
const renamedReader = await readFile(renamedReaderPath, "utf8");
await writeFile(
  renamedReaderPath,
  renamedReader
    .replace("import { total }", "import { total as localTotal }")
    .replace("return total;", "return localTotal;")
);
const renamed = await validate(renamedSource, renamedSpec, renamedDir);
assert.equal(renamed.result, "proved under assumptions", JSON.stringify(renamed));
assert.equal(renamed.outputAst.stateModule, "./store.mjs");
assert.equal(renamed.outputAst.currentRef, "localTotal");
assert.equal(renamed.outputAst.importedName, "total");
