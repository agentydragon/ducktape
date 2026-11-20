import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

if (process.env.ACORN_RLOCATION) {
  process.env.JS_MODULE_PROOF_ACORN = resolve(process.env.TEST_SRCDIR, process.env.ACORN_RLOCATION);
}
process.env.JS_MODULE_PROOF_LEAN = resolve(process.env.TEST_SRCDIR, process.env.LEAN_RLOCATION);
const { generateEffects, validateEffects } = await import("./effects.mjs");
const here = dirname(fileURLToPath(import.meta.url));
const dir = await mkdtemp(join(process.env.TEST_TMPDIR, "init-order-"));
const original = join(dir, "original.mjs");
await writeFile(
  original,
  `
globalThis.record("first");
globalThis.record("middle");
globalThis.record("last");
`
);

const specFor = (first, second) => ({
  kind: "effects",
  entry: "entry.mjs",
  modules: {
    "a.mjs": { imports: [], statements: first },
    "b.mjs": { imports: ["a.mjs"], statements: second },
    "entry.mjs": { imports: ["b.mjs"], statements: [] },
  },
});

async function prepare(label, spec) {
  const specPath = join(dir, `${label}.json`);
  const output = join(dir, label);
  await writeFile(specPath, JSON.stringify(spec));
  await generateEffects(original, specPath, output);
  return { specPath, output };
}

const unsafe = await prepare("unsafe", specFor([0, 2], [1]));
const rejected = await validateEffects(original, unsafe.specPath, unsafe.output);
assert.equal(rejected.result, "unsafe", JSON.stringify(rejected));
assert.deepEqual(rejected.original, ["first", "middle", "last"]);
assert.deepEqual(rejected.output, ["first", "last", "middle"]);
assert.deepEqual(rejected.modeledOutput, rejected.output);
assert.match(rejected.certificate, /theorem effect_output_trace/);
assert.match(rejected.certificate, /theorem effect_machine_trace/);
assert.match(rejected.certificate, /theorem effect_trace_differs/);
const model =
  (await readFile(join(here, "Semantics.lean"), "utf8")) + "\n" + (await readFile(join(here, "Effects.lean"), "utf8"));
const replayedProof = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
  encoding: "utf8",
  input: model + rejected.certificate,
});
assert.equal(replayedProof.status, 0, replayedProof.stderr || replayedProof.stdout);
assert.match(replayedProof.stdout, /'effect_output_trace' does not depend on any axioms/);
assert.match(replayedProof.stdout, /'effect_machine_trace' does not depend on any axioms/);
assert.match(replayedProof.stdout, /'effect_trace_differs' does not depend on any axioms/);

const safe = await prepare("safe", specFor([0], [1, 2]));
const checked = await validateEffects(original, safe.specPath, safe.output);
assert.equal(checked.result, "proved under assumptions", JSON.stringify(checked));
assert.match(checked.certificate, /theorem effect_behavior/);

function run(path, throwAt) {
  const code = `const events=[]; globalThis.record=value=>{ events.push(value); if (events.length === ${JSON.stringify(throwAt)}) throw "host-error"; }; import(${JSON.stringify(pathToFileURL(path).href)}).then(()=>console.log(JSON.stringify({events, error:null})), error=>console.log(JSON.stringify({events, error})));`;
  const result = spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

for (const throwAt of [0, 1, 2, 3]) {
  assert.deepEqual(run(original, throwAt), run(join(safe.output, "entry.mjs"), throwAt));
}

const diamond = await prepare("diamond", {
  kind: "effects",
  entry: "entry.mjs",
  modules: {
    "a.mjs": { imports: [], statements: [0] },
    "b.mjs": { imports: ["a.mjs"], statements: [1] },
    "c.mjs": { imports: ["a.mjs"], statements: [2] },
    "entry.mjs": { imports: ["b.mjs", "c.mjs"], statements: [] },
  },
});
const diamondResult = await validateEffects(original, diamond.specPath, diamond.output);
assert.equal(diamondResult.result, "proved under assumptions", JSON.stringify(diamondResult));
assert.deepEqual(run(original, 0), run(join(diamond.output, "entry.mjs"), 0));

const aPath = join(safe.output, "a.mjs");
const a = await readFile(aPath, "utf8");
await writeFile(aPath, a.replace("first", "changed"));
const wrongAssignment = await validateEffects(original, safe.specPath, safe.output);
assert.equal(wrongAssignment.result, "unknown");
assert.match(wrongAssignment.reason, /graph and assignment/);
assert.deepEqual(wrongAssignment.modeledOutput, ["changed", "middle", "last"]);
assert.deepEqual(run(join(safe.output, "entry.mjs"), 0).events, wrongAssignment.modeledOutput);
await writeFile(aPath, a);

const cyclicSpec = specFor([0], [1, 2]);
cyclicSpec.modules["a.mjs"].imports = ["b.mjs"];
const cyclic = await prepare("cyclic", cyclicSpec);
const cyclicResult = await validateEffects(original, cyclic.specPath, cyclic.output);
assert.equal(cyclicResult.result, "unknown");
assert.match(cyclicResult.reason, /graph and assignment/);

const cli = spawnSync(process.execPath, [join(here, "main.mjs"), "validate", original, safe.specPath, safe.output], {
  encoding: "utf8",
});
assert.equal(cli.status, 0, cli.stderr);
assert.equal(JSON.parse(cli.stdout).result, "proved under assumptions");
