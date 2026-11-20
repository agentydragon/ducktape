import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

if (process.env.ACORN_RLOCATION)
  process.env.JS_MODULE_PROOF_ACORN = resolve(process.env.TEST_SRCDIR, process.env.ACORN_RLOCATION);
process.env.JS_MODULE_PROOF_LEAN = resolve(process.env.TEST_SRCDIR, process.env.LEAN_RLOCATION);
if (process.env.TEST_SHARD_STATUS_FILE) await writeFile(process.env.TEST_SHARD_STATUS_FILE, "");
const { execute } = await import("./fuzz.mjs");
const here = dirname(fileURLToPath(import.meta.url));
const root = await mkdtemp(join(process.env.TEST_TMPDIR, "closure-proof-"));
const source = join(root, "source.mjs");
const spec = join(root, "spec.json");
const out = join(root, "out");
const unsafe = Number(process.env.TEST_SHARD_INDEX ?? 0) === 1;
await writeFile(
  source,
  `let seed = 1;
function makeCounter() { let value = seed; return () => ++value; }
const next = makeCounter();
export function read() { return next(); }
read();
globalThis.record(read());
`
);
const modules = {
  "state.mjs": { imports: [], statements: [0, 1, 2] },
  "logic.mjs": { imports: ["state.mjs"], statements: unsafe ? [3] : [3, 4] },
  "observer.mjs": { imports: ["logic.mjs"], statements: [5] },
  "entry.mjs": { imports: ["logic.mjs", "observer.mjs"], statements: unsafe ? [4] : [] },
};
await writeFile(spec, JSON.stringify({ kind: "bindings", entry: "entry.mjs", modules }));
function cli(...args) {
  const child = spawnSync(process.execPath, [join(here, "main.mjs"), ...args], { encoding: "utf8" });
  const value = JSON.parse(child.stdout || child.stderr);
  assert.equal(child.status, value.result === "unknown" ? 2 : 0, JSON.stringify(value));
  return value;
}
assert.equal(cli("generate", source, spec, out).result, "generated");
const result = cli("validate", source, spec, out);
assert.equal(result.result, unsafe ? "unsafe" : "proved under assumptions", JSON.stringify(result));
const calls = ["read", "read"];
const before = execute(source, calls);
const after = execute(join(out, "entry.mjs"), calls);
assert.deepEqual(before.results, [{ value: 4 }, { value: 5 }]);
assert.deepEqual(after.results, before.results);
assert.deepEqual(before.events, [3]);
assert.deepEqual(after.events, unsafe ? [2] : [3]);
if (unsafe) {
  assert.deepEqual(result.original, { events: [3], exports: ["read"] });
  assert.deepEqual(result.output, { events: [2], exports: ["read"] });
}
const models = (
  await Promise.all(["Semantics.lean", "Effects.lean", "Bindings.lean"].map((n) => readFile(join(here, n), "utf8")))
).join("\n");
const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["--trust=0", "--stdin"], {
  encoding: "utf8",
  input:
    models +
    result.certificate +
    `
example : resolveLexical [[⟨"x", 7, false⟩], [⟨"x", 8, true⟩]] "x" = .temporalDeadZone 7 := by decide
example : resolveLexical [[⟨"y", 7, false⟩], [⟨"x", 8, true⟩]] "x" = .binding 8 := by decide
#print axioms lexical_nearest_binding
`,
});
assert.equal(replay.status, 0, replay.stdout + replay.stderr);
assert.match(replay.stdout, /'lexical_nearest_binding' does not depend on any axioms/);
assert.match(
  replay.stdout,
  unsafe ? /'binding_difference' does not depend on any axioms/ : /'binding_future_calls' does not depend on any axioms/
);
if (!unsafe) {
  const file = join(out, "state.mjs");
  const text = await readFile(file, "utf8");
  await writeFile(file, text.replace("++value", "value++"));
  assert.equal(cli("validate", source, spec, out).result, "unknown");
}

if (!unsafe) {
  const multiSource = join(root, "multi.mjs");
  const multiSpec = join(root, "multi.json");
  const multiOut = join(root, "multi-out");
  await writeFile(
    multiSource,
    `let seed = 1;
function make() { let value = seed; return () => ++value; }
const first = make();
const second = make();
function readFirst() { return first(); }
function readSecond() { return second(); }
export { readFirst, readSecond };
`
  );
  await writeFile(
    multiSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1, 2, 3] },
        "reader.mjs": { imports: ["state.mjs"], statements: [4, 5] },
        "entry.mjs": { imports: ["reader.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", multiSource, multiSpec, multiOut).result, "generated");
  assert.equal(cli("validate", multiSource, multiSpec, multiOut).result, "proved under assumptions");
  const multiCalls = ["readFirst", "readFirst", "readSecond"];
  const sourceTrace = execute(multiSource, multiCalls);
  assert.deepEqual(sourceTrace.results, [{ value: 2 }, { value: 3 }, { value: 2 }]);
  assert.deepEqual(execute(join(multiOut, "entry.mjs"), multiCalls), sourceTrace);
  const shadowSource = join(root, "shadow.mjs");
  await writeFile(
    shadowSource,
    (await readFile(source, "utf8")).replace("let value = seed", "let seed = seed").replace("++value", "++seed")
  );
  assert.equal(cli("generate", shadowSource, spec, join(root, "shadow-out")).result, "unknown");
}
