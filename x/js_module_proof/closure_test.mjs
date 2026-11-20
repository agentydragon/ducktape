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
const { validateBindings } = await import("./bindings.mjs");
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
  const shadowOut = join(root, "shadow-out");
  assert.equal(cli("generate", shadowSource, spec, shadowOut).result, "generated");
  assert.equal(cli("validate", shadowSource, spec, shadowOut).result, "unknown");
  assert.equal(execute(shadowSource, []).error?.name, "ReferenceError");
  assert.equal(execute(join(shadowOut, "entry.mjs"), []).error?.name, "ReferenceError");
}

if (unsafe) {
  const laterSource = join(root, "later.mjs");
  const laterSpec = join(root, "later.json");
  const laterOut = join(root, "later-out");
  await writeFile(
    laterSource,
    `let seed = 1;
let amount = 1;
function bump() { seed += amount; }
function make() { let value = seed; return () => ++value; }
bump();
const next = make();
function read() { return next(); }
export { read };
`
  );
  await writeFile(
    laterSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1, 2] },
        "factory.mjs": { imports: ["state.mjs"], statements: [3, 5] },
        "reader.mjs": { imports: ["factory.mjs"], statements: [6] },
        "entry.mjs": { imports: ["state.mjs", "reader.mjs"], statements: [4] },
      },
    })
  );
  assert.equal(cli("generate", laterSource, laterSpec, laterOut).result, "generated");
  const witness = cli("validate", laterSource, laterSpec, laterOut);
  assert.equal(witness.result, "unsafe", JSON.stringify(witness));
  assert.deepEqual(witness.calls, ["read"]);
  assert.deepEqual(witness.original, { events: [], exports: ["read"], results: [3] });
  assert.deepEqual(witness.output, { events: [], exports: ["read"], results: [2] });
  assert.deepEqual(execute(laterSource, witness.calls).results, [{ value: 3 }]);
  assert.deepEqual(execute(join(laterOut, "entry.mjs"), witness.calls).results, [{ value: 2 }]);
  const witnessReplay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + witness.certificate,
  });
  assert.equal(witnessReplay.status, 0, witnessReplay.stdout + witnessReplay.stderr);
  assert.match(witnessReplay.stdout, /'binding_witness_original' does not depend on any axioms/);
  assert.match(witnessReplay.stdout, /'binding_witness_output' does not depend on any axioms/);
  assert.match(witnessReplay.stdout, /'binding_witness_difference' does not depend on any axioms/);
  const combinedSource = join(root, "later-parameter-branch.mjs");
  const combinedSpec = join(root, "later-parameter-branch.json");
  const combinedOut = join(root, "later-parameter-branch-out");
  await writeFile(
    combinedSource,
    (await readFile(laterSource, "utf8")).replace(
      "export { read };",
      "function choose(flag, value) { if (flag) { return seed + value; } else { return value + 1; } }\nexport { read, choose };"
    )
  );
  const combinedAssignment = JSON.parse(await readFile(laterSpec, "utf8"));
  combinedAssignment.modules["reader.mjs"].statements.push(7);
  combinedAssignment.modules["reader.mjs"].imports.push("state.mjs");
  await writeFile(combinedSpec, JSON.stringify(combinedAssignment));
  assert.equal(cli("generate", combinedSource, combinedSpec, combinedOut).result, "generated");
  const combinedWitness = cli("validate", combinedSource, combinedSpec, combinedOut);
  assert.equal(combinedWitness.result, "unsafe", JSON.stringify(combinedWitness));
  assert.deepEqual(combinedWitness.calls, [{ name: "read", args: [] }]);
  assert.deepEqual(combinedWitness.original.results, [3]);
  assert.deepEqual(combinedWitness.output.results, [2]);
  const combinedReplay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + combinedWitness.certificate,
  });
  assert.equal(combinedReplay.status, 0, combinedReplay.stdout + combinedReplay.stderr);
  assert.match(combinedReplay.stdout, /'binding_witness_difference' does not depend on any axioms/);
  const branchCalls = [
    { name: "choose", args: [0, 4] },
    { name: "choose", args: [1, 4] },
  ];
  assert.deepEqual(execute(combinedSource, branchCalls).results, [{ value: 5 }, { value: 6 }]);
  assert.deepEqual(execute(join(combinedOut, "entry.mjs"), branchCalls), execute(combinedSource, branchCalls));
}

if (!unsafe) {
  const throwSource = join(root, "throw-source.mjs");
  const throwSpec = join(root, "throw-spec.json");
  const throwOut = join(root, "throw-out");
  await writeFile(
    throwSource,
    `let value = 1;
function read() { return value; }
function fail() { throw 7; }
export { read, fail };
`
  );
  await writeFile(
    throwSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0] },
        "read.mjs": { imports: ["state.mjs"], statements: [1] },
        "fail.mjs": { imports: [], statements: [2] },
        "entry.mjs": { imports: ["read.mjs", "fail.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", throwSource, throwSpec, throwOut).result, "generated");
  const proof = cli("validate", throwSource, throwSpec, throwOut);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = ["read", "fail", "read"];
  const beforeThrow = execute(throwSource, calls);
  assert.deepEqual(beforeThrow.results, [{ value: 1 }]);
  assert.deepEqual(beforeThrow.error, { name: null, phase: "call", index: 1, thrown: { kind: "number", value: 7 } });
  assert.deepEqual(execute(join(throwOut, "entry.mjs"), calls), beforeThrow);
  const throwReplay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + proof.certificate,
  });
  assert.equal(throwReplay.status, 0, throwReplay.stdout + throwReplay.stderr);
  assert.match(throwReplay.stdout, /'binding_throw_values' does not depend on any axioms/);
  const emitted = join(throwOut, "fail.mjs");
  await writeFile(emitted, (await readFile(emitted, "utf8")).replace("throw 7", "throw 8"));
  assert.equal(cli("validate", throwSource, throwSpec, throwOut).result, "unknown");
  assert.notDeepEqual(execute(join(throwOut, "entry.mjs"), calls).error, beforeThrow.error);
}

if (!unsafe) {
  const scopeSource = join(root, "scope-source.mjs");
  const scopeSpec = join(root, "scope-spec.json");
  const scopeOut = join(root, "scope-out");
  await writeFile(
    scopeSource,
    `let seed = 4;
function calculate(arg) {
  let value = seed;
  function nested(seed) { return seed + value; }
  if (arg) { let value = arg; return nested(value); }
  return nested(1);
}
function read() { return calculate(3); }
export { read };
`
  );
  const scopeModules = {
    "state.mjs": { imports: [], statements: [0] },
    "logic.mjs": { imports: ["state.mjs"], statements: [1] },
    "reader.mjs": { imports: ["logic.mjs"], statements: [2] },
    "entry.mjs": { imports: ["reader.mjs"], statements: [] },
  };
  await writeFile(scopeSpec, JSON.stringify({ kind: "bindings", entry: "entry.mjs", modules: scopeModules }));
  assert.equal(cli("generate", scopeSource, scopeSpec, scopeOut).result, "generated");
  assert.equal(cli("validate", scopeSource, scopeSpec, scopeOut).result, "unknown");
  assert.equal((await validateBindings(scopeSource, scopeSpec, scopeOut)).result, "unknown");
  assert.deepEqual(execute(scopeSource, ["read"]).results, [{ value: 7 }]);
  assert.deepEqual(execute(join(scopeOut, "entry.mjs"), ["read"]), execute(scopeSource, ["read"]));
  assert.match(await readFile(join(scopeOut, "logic.mjs"), "utf8"), /import \{ seed \}/);
  const writeSource = join(root, "scope-write-source.mjs");
  await writeFile(
    writeSource,
    (await readFile(scopeSource, "utf8")).replace("let value = seed;", "seed += arg; let value = seed;")
  );
  assert.equal(cli("generate", writeSource, scopeSpec, join(root, "scope-write-out")).result, "unknown");
}

if (!unsafe) {
  const parameterSource = join(root, "parameter-source.mjs");
  const parameterSpec = join(root, "parameter-spec.json");
  const parameterOut = join(root, "parameter-out");
  await writeFile(
    parameterSource,
    `let seed = 4;
function shadow(seed, delta) { return seed + delta; }
function add(a, b) { return (a + b) + seed; }
function choose(flag, value) { if (flag) { return value + seed; } else { return value + 1; } }
function local(flag, value) { let total = value + seed; if (flag) { let value = 2; return total + value; } else { return total + value; } }
function maybe(flag) { if (flag) { throw seed + flag; } else { return seed; } }
function text(flag) { if (flag) { throw "oops"; } else { return seed; } }
function boolean(flag) { if (flag) { throw false; } else { return seed; } }
function nil(flag) { if (flag) { throw null; } else { return seed; } }
function undef(flag) { if (flag) { throw void 0; } else { return seed; } }
export { shadow, add, choose, local, maybe, text, boolean, nil, undef };
`
  );
  await writeFile(
    parameterSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0] },
        "logic.mjs": { imports: ["state.mjs"], statements: [1, 2, 3, 4, 5, 6, 7, 8, 9] },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", parameterSource, parameterSpec, parameterOut).result, "generated");
  const proof = cli("validate", parameterSource, parameterSpec, parameterOut);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = [
    { name: "shadow", args: [8, 2] },
    { name: "add", args: [1, 3] },
    { name: "add", args: [4, 5] },
    { name: "choose", args: [0, 5] },
    { name: "choose", args: [1, 5] },
    { name: "local", args: [0, 3] },
    { name: "local", args: [1, 3] },
  ];
  const sourceTrace = execute(parameterSource, calls);
  assert.deepEqual(sourceTrace.results, [
    { value: 10 },
    { value: 8 },
    { value: 13 },
    { value: 6 },
    { value: 9 },
    { value: 10 },
    { value: 9 },
  ]);
  assert.deepEqual(execute(join(parameterOut, "entry.mjs"), calls), sourceTrace);
  const throwingCalls = [
    { name: "maybe", args: [0] },
    { name: "maybe", args: [1] },
    { name: "maybe", args: [0] },
  ];
  const throwingTrace = execute(parameterSource, throwingCalls);
  assert.deepEqual(throwingTrace.results, [{ value: 4 }]);
  assert.deepEqual(throwingTrace.error, { name: null, phase: "call", index: 1, thrown: { kind: "number", value: 5 } });
  assert.deepEqual(execute(join(parameterOut, "entry.mjs"), throwingCalls), throwingTrace);
  for (const [name, thrown] of [
    ["text", { kind: "string", value: "oops" }],
    ["boolean", { kind: "boolean", value: false }],
    ["nil", { kind: "null" }],
    ["undef", { kind: "undefined" }],
  ]) {
    const primitiveCalls = [
      { name, args: [0] },
      { name, args: [1] },
    ];
    const primitiveTrace = execute(parameterSource, primitiveCalls);
    assert.deepEqual(primitiveTrace.results, [{ value: 4 }]);
    assert.deepEqual(primitiveTrace.error, { name: null, phase: "call", index: 1, thrown });
    assert.deepEqual(execute(join(parameterOut, "entry.mjs"), primitiveCalls), primitiveTrace);
  }
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + proof.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_future_calls' does not depend on any axioms/);
  const tdzSource = join(root, "local-tdz-source.mjs");
  const tdzSpec = join(root, "local-tdz-spec.json");
  const tdzOut = join(root, "local-tdz-out");
  await writeFile(
    tdzSource,
    `let later = 4;
function broken() { let first = later; let later = 1; return first; }
export { broken };
`
  );
  await writeFile(
    tdzSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0] },
        "logic.mjs": { imports: ["state.mjs"], statements: [1] },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", tdzSource, tdzSpec, tdzOut).result, "generated");
  assert.equal(cli("validate", tdzSource, tdzSpec, tdzOut).result, "unknown");
  assert.equal(execute(tdzSource, ["broken"]).error.name, "ReferenceError");
  assert.equal(execute(join(tdzOut, "entry.mjs"), ["broken"]).error.name, "ReferenceError");
  const logic = join(parameterOut, "logic.mjs");
  await writeFile(logic, (await readFile(logic, "utf8")).replace("(a + b) + seed", "(a + b) + a"));
  assert.equal(cli("validate", parameterSource, parameterSpec, parameterOut).result, "unknown");
  assert.notDeepEqual(execute(join(parameterOut, "entry.mjs"), calls), sourceTrace);
}
