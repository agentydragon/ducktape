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
const shard = Number(process.env.TEST_SHARD_INDEX ?? 0);
const unsafe = shard === 1;
function cli(...args) {
  const child = spawnSync(process.execPath, [join(here, "main.mjs"), ...args], { encoding: "utf8" });
  const value = JSON.parse(child.stdout || child.stderr);
  assert.equal(child.status, value.result === "unknown" ? 2 : 0, JSON.stringify(value));
  return value;
}
const models = (
  await Promise.all(["Semantics.lean", "Effects.lean", "Bindings.lean"].map((n) => readFile(join(here, n), "utf8")))
).join("\n");
if (shard < 2) {
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

  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
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
    unsafe
      ? /'binding_difference' does not depend on any axioms/
      : /'binding_future_calls' does not depend on any axioms/
  );
  if (!unsafe) {
    const file = join(out, "state.mjs");
    const text = await readFile(file, "utf8");
    await writeFile(file, text.replace("++value", "value++"));
    assert.equal(cli("validate", source, spec, out).result, "unknown");
  }
}

if (shard === 0) {
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
  const witnessReplay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + witness.certificate,
  });
  assert.equal(witnessReplay.status, 0, witnessReplay.stdout + witnessReplay.stderr);
  assert.match(witnessReplay.stdout, /'binding_witness_original' does not depend on any axioms/);
  assert.match(witnessReplay.stdout, /'binding_witness_output' does not depend on any axioms/);
  assert.match(witnessReplay.stdout, /'binding_witness_difference' does not depend on any axioms/);
}

if (shard === 0) {
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
  const throwReplay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
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

if (shard === 0) {
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

if (shard === 2) {
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
function update(value) { let total = value; total += seed; total++; return total; }
export { shadow, add, choose, local, update };
`
  );
  await writeFile(
    parameterSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0] },
        "logic.mjs": { imports: ["state.mjs"], statements: [1, 2, 3, 4, 5] },
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
    { name: "update", args: [3] },
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
    { value: 8 },
  ]);
  assert.deepEqual(execute(join(parameterOut, "entry.mjs"), calls), sourceTrace);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
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
  const immutableSource = join(root, "immutable-local.mjs");
  const immutableSpec = join(root, "immutable-local.json");
  const immutableOut = join(root, "immutable-local-out");
  await writeFile(
    immutableSource,
    `function broken(value) { const local = value; local += 1; return local; }
export { broken };
`
  );
  await writeFile(
    immutableSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "entry.mjs": { imports: [], statements: [0] },
      },
    })
  );
  assert.equal(cli("generate", immutableSource, immutableSpec, immutableOut).result, "generated");
  assert.equal(cli("validate", immutableSource, immutableSpec, immutableOut).result, "unknown");
  assert.equal(execute(immutableSource, [{ name: "broken", args: [1] }]).error.name, "TypeError");
  const logic = join(parameterOut, "logic.mjs");
  await writeFile(logic, (await readFile(logic, "utf8")).replace("(a + b) + seed", "(a + b) + a"));
  assert.equal(cli("validate", parameterSource, parameterSpec, parameterOut).result, "unknown");
  assert.notDeepEqual(execute(join(parameterOut, "entry.mjs"), calls), sourceTrace);
}

if (shard === 3) {
  const primitiveSource = join(root, "primitive-source.mjs");
  const primitiveSpec = join(root, "primitive-spec.json");
  const primitiveOut = join(root, "primitive-out");
  await writeFile(
    primitiveSource,
    `let seed = 4;
function maybe(flag) { if (flag) { throw seed + flag; } else { return seed; } }
function text(flag) { if (flag) { throw "oops"; } else { return seed; } }
function boolean(flag) { if (flag) { throw false; } else { return seed; } }
function nil(flag) { if (flag) { throw null; } else { return seed; } }
function undef(flag) { if (flag) { throw void 0; } else { return seed; } }
export { maybe, text, boolean, nil, undef };
`
  );
  await writeFile(
    primitiveSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0] },
        "logic.mjs": { imports: ["state.mjs"], statements: [1, 2, 3, 4, 5] },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", primitiveSource, primitiveSpec, primitiveOut).result, "generated");
  const proof = cli("validate", primitiveSource, primitiveSpec, primitiveOut);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const throwingCalls = [
    { name: "maybe", args: [0] },
    { name: "maybe", args: [1] },
    { name: "maybe", args: [0] },
  ];
  const throwingTrace = execute(primitiveSource, throwingCalls);
  assert.deepEqual(throwingTrace.results, [{ value: 4 }]);
  assert.deepEqual(throwingTrace.error, { name: null, phase: "call", index: 1, thrown: { kind: "number", value: 5 } });
  assert.deepEqual(execute(join(primitiveOut, "entry.mjs"), throwingCalls), throwingTrace);
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
    const primitiveTrace = execute(primitiveSource, primitiveCalls);
    assert.deepEqual(primitiveTrace.results, [{ value: 4 }]);
    assert.deepEqual(primitiveTrace.error, { name: null, phase: "call", index: 1, thrown });
    assert.deepEqual(execute(join(primitiveOut, "entry.mjs"), primitiveCalls), primitiveTrace);
  }
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + proof.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_throw_values' does not depend on any axioms/);
}

if (shard === 4) {
  const callSource = join(root, "call-source.mjs");
  const callSpec = join(root, "call-spec.json");
  const callOut = join(root, "call-out");
  await writeFile(
    callSource,
    `let seed = 4;
function helper(value) { if (value) { return value + seed; } else { throw "bad"; } }
function call(value) { return helper(value) + 1; }
export { call };
`
  );
  await writeFile(
    callSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "logic.mjs": { imports: ["state.mjs"], statements: [2] },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", callSource, callSpec, callOut).result, "generated");
  const proof = cli("validate", callSource, callSpec, callOut);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = [
    { name: "call", args: [3] },
    { name: "call", args: [7] },
  ];
  const sourceTrace = execute(callSource, calls);
  assert.deepEqual(sourceTrace.results, [{ value: 8 }, { value: 12 }]);
  assert.deepEqual(execute(join(callOut, "entry.mjs"), calls), sourceTrace);
  const thrownCalls = [
    { name: "call", args: [3] },
    { name: "call", args: [0] },
    { name: "call", args: [7] },
  ];
  const thrownTrace = execute(callSource, thrownCalls);
  assert.deepEqual(thrownTrace.results, [{ value: 8 }]);
  assert.deepEqual(thrownTrace.error, {
    name: null,
    phase: "call",
    index: 1,
    thrown: { kind: "string", value: "bad" },
  });
  assert.deepEqual(execute(join(callOut, "entry.mjs"), thrownCalls), thrownTrace);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + proof.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_future_calls' does not depend on any axioms/);
  const state = join(callOut, "state.mjs");
  await writeFile(state, (await readFile(state, "utf8")).replace("return value + seed", "return value + 1"));
  assert.equal(cli("validate", callSource, callSpec, callOut).result, "unknown");
  assert.notDeepEqual(execute(join(callOut, "entry.mjs"), calls), sourceTrace);
  const recursiveSource = join(root, "recursive-source.mjs");
  const recursiveSpec = join(root, "recursive-spec.json");
  const recursiveOut = join(root, "recursive-out");
  await writeFile(
    recursiveSource,
    `function first(value) { return second(value); }
function second(value) { return first(value); }
export { first };
`
  );
  await writeFile(
    recursiveSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "entry.mjs": { imports: [], statements: [0, 1] },
      },
    })
  );
  assert.equal(cli("generate", recursiveSource, recursiveSpec, recursiveOut).result, "generated");
  assert.equal(cli("validate", recursiveSource, recursiveSpec, recursiveOut).result, "unknown");
}

if (shard === 5) {
  const combinedSource = join(root, "later-parameter-branch.mjs");
  const combinedSpec = join(root, "later-parameter-branch.json");
  const combinedOut = join(root, "later-parameter-branch-out");
  await writeFile(
    combinedSource,
    `let seed = 1;
let amount = 1;
function bump() { seed += amount; }
function make() { let value = seed; return () => ++value; }
bump();
const next = make();
function read() { return next(); }
function choose(flag, value) { if (flag) { return seed + value; } else { return value + 1; } }
export { read, choose };
`
  );
  const combinedAssignment = {
    kind: "bindings",
    entry: "entry.mjs",
    modules: {
      "state.mjs": { imports: [], statements: [0, 1, 2] },
      "factory.mjs": { imports: ["state.mjs"], statements: [3, 5] },
      "reader.mjs": { imports: ["factory.mjs", "state.mjs"], statements: [6, 7] },
      "entry.mjs": { imports: ["state.mjs", "reader.mjs"], statements: [4] },
    },
  };
  await writeFile(combinedSpec, JSON.stringify(combinedAssignment));
  assert.equal(cli("generate", combinedSource, combinedSpec, combinedOut).result, "generated");
  const combinedWitness = cli("validate", combinedSource, combinedSpec, combinedOut);
  assert.equal(combinedWitness.result, "unsafe", JSON.stringify(combinedWitness));
  assert.deepEqual(combinedWitness.calls, [{ name: "read", args: [] }]);
  assert.deepEqual(combinedWitness.original.results, [3]);
  assert.deepEqual(combinedWitness.output.results, [2]);
  const combinedReplay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
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

if (shard === 6) {
  await writeFile(
    source,
    `let seed = 1;
function make(start) { let value = start + seed; return (step) => { value += step; return value; }; }
const first = make(2);
const second = make(3);
export { first, second };
`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "closures.mjs": { imports: ["state.mjs"], statements: [2, 3] },
        "entry.mjs": { imports: ["closures.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).result, "generated");
  const proof = cli("validate", source, spec, out);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = [
    { name: "first", args: [2] },
    { name: "first", args: [3] },
    { name: "second", args: [1] },
  ];
  const trace = execute(source, calls);
  assert.deepEqual(trace.results, [{ value: 5 }, { value: 8 }, { value: 5 }]);
  assert.deepEqual(execute(join(out, "entry.mjs"), calls), trace);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + proof.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_future_calls' does not depend on any axioms/);
  const immutable = join(root, "captured-immutable.mjs");
  const immutableOut = join(root, "captured-immutable-out");
  await writeFile(immutable, (await readFile(source, "utf8")).replace("let value =", "const value ="));
  assert.equal(cli("generate", immutable, spec, immutableOut).result, "generated");
  assert.equal(cli("validate", immutable, spec, immutableOut).result, "unknown");
  assert.equal(execute(immutable, calls).error.name, "TypeError");
  const tdz = join(root, "captured-tdz.mjs");
  const tdzOut = join(root, "captured-tdz-out");
  await writeFile(
    tdz,
    (await readFile(source, "utf8")).replace("let value = start + seed;", "let value = seed; let seed = 1;")
  );
  assert.equal(cli("generate", tdz, spec, tdzOut).result, "generated");
  assert.equal(cli("validate", tdz, spec, tdzOut).result, "unknown");
  assert.equal(execute(tdz, calls).error.name, "ReferenceError");
}

if (shard === 7) {
  await writeFile(
    source,
    `let seed = 1;
let amount = 1;
function bump() { seed += amount; }
function make(start) { let value = start + seed; return (step) => { value += step; return value; }; }
bump();
const first = make(2);
export { first };
`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1, 2, 3] },
        "closures.mjs": { imports: ["state.mjs"], statements: [5] },
        "entry.mjs": { imports: ["state.mjs", "closures.mjs"], statements: [4] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).result, "generated");
  const witness = cli("validate", source, spec, out);
  assert.equal(witness.result, "unsafe", JSON.stringify(witness));
  assert.deepEqual(witness.calls, [{ name: "first", args: [0] }]);
  assert.deepEqual(execute(source, witness.calls).results, [{ value: 4 }]);
  assert.deepEqual(execute(join(out, "entry.mjs"), witness.calls).results, [{ value: 3 }]);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + witness.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_witness_difference' does not depend on any axioms/);
}

if (shard === 8) {
  await writeFile(
    source,
    `let seed = 1;
function make(start) { let value = start + seed; return (step) => { value += step; return value; }; }
const first = make(2);
const second = make(5);
const alias = first;
const alias2 = alias;
export { first, second, alias, alias2 };
`
  );
  const modules = {
    "state.mjs": { imports: [], statements: [0, 1, 2, 3] },
    "alias.mjs": { imports: ["state.mjs"], statements: [4] },
    "entry.mjs": { imports: ["state.mjs", "alias.mjs"], statements: [5] },
  };
  await writeFile(spec, JSON.stringify({ kind: "bindings", entry: "entry.mjs", modules }));
  assert.equal(cli("generate", source, spec, out).result, "generated");
  const proof = cli("validate", source, spec, out);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = [
    { name: "first", args: [2] },
    { name: "alias", args: [3] },
    { name: "first", args: [1] },
    { name: "alias2", args: [4] },
    { name: "second", args: [1] },
  ];
  const trace = execute(source, calls);
  assert.deepEqual(trace.results, [{ value: 5 }, { value: 8 }, { value: 9 }, { value: 13 }, { value: 7 }]);
  assert.deepEqual(execute(join(out, "entry.mjs"), calls), trace);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + proof.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_future_calls' does not depend on any axioms/);
  const emitted = join(out, "alias.mjs");
  await writeFile(emitted, (await readFile(emitted, "utf8")).replaceAll("first", "second"));
  assert.equal(cli("validate", source, spec, out).result, "unknown");
  assert.notDeepEqual(execute(join(out, "entry.mjs"), calls), trace);
  const tdzSource = join(root, "alias-tdz.mjs");
  const tdzSpec = join(root, "alias-tdz.json");
  const tdzOut = join(root, "alias-tdz-out");
  await writeFile(
    tdzSource,
    (await readFile(source, "utf8")).replace(
      "const first = make(2);\nconst second = make(5);\nconst alias = first;",
      "const alias = first;\nconst first = make(2);\nconst second = make(5);"
    )
  );
  await writeFile(
    tdzSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1, 3, 4] },
        "alias.mjs": { imports: ["state.mjs"], statements: [2] },
        "entry.mjs": { imports: ["state.mjs", "alias.mjs"], statements: [5] },
      },
    })
  );
  assert.equal(cli("generate", tdzSource, tdzSpec, tdzOut).result, "generated");
  assert.equal(cli("validate", tdzSource, tdzSpec, tdzOut).result, "unknown");
  assert.equal(execute(tdzSource, []).error.name, "ReferenceError");
  const cycleSource = join(root, "alias-cycle.mjs");
  const cycleSpec = join(root, "alias-cycle.json");
  const cycleOut = join(root, "alias-cycle-out");
  await writeFile(cycleSource, "const first = second; const second = first; export { first };\n");
  await writeFile(
    cycleSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "entry.mjs": { imports: [], statements: [0, 1] },
      },
    })
  );
  assert.equal(cli("generate", cycleSource, cycleSpec, cycleOut).result, "generated");
  assert.equal(cli("validate", cycleSource, cycleSpec, cycleOut).result, "unknown");
  assert.equal(execute(cycleSource, []).error.name, "ReferenceError");
}

if (shard === 9) {
  await writeFile(
    source,
    `let seed = 1;
let amount = 1;
function bump() { seed += amount; }
function make(start) { let value = start + seed; return (step) => { value += step; return value; }; }
bump();
const first = make(2);
const alias = first;
const alias2 = alias;
export { alias2 };
`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1, 2, 3] },
        "closures.mjs": { imports: ["state.mjs"], statements: [5, 6, 7] },
        "entry.mjs": { imports: ["state.mjs", "closures.mjs"], statements: [4] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).result, "generated");
  const witness = cli("validate", source, spec, out);
  assert.equal(witness.result, "unsafe", JSON.stringify(witness));
  assert.deepEqual(witness.calls, [{ name: "alias2", args: [0] }]);
  assert.deepEqual(execute(source, witness.calls).results, [{ value: 4 }]);
  assert.deepEqual(execute(join(out, "entry.mjs"), witness.calls).results, [{ value: 3 }]);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + witness.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_witness_difference' does not depend on any axioms/);
}

if (shard === 10) {
  await writeFile(
    source,
    `let seed = 4;
function product(value, factor) { let total = value; total *= factor; return total * seed; }
function maybe(flag) { if (flag) { throw 7; } else { return seed; } }
function invoke(flag) { return maybe(flag) * 2; }
export { product, invoke };
`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 2] },
        "logic.mjs": { imports: ["state.mjs"], statements: [1, 3] },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).result, "generated");
  const proof = cli("validate", source, spec, out);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = [
    { name: "product", args: [2, 3] },
    { name: "invoke", args: [0] },
    { name: "invoke", args: [1] },
  ];
  const trace = execute(source, calls);
  assert.deepEqual(trace.results, [{ value: 24 }, { value: 8 }]);
  assert.deepEqual(trace.error, { name: null, phase: "call", index: 2, thrown: { kind: "number", value: 7 } });
  assert.deepEqual(execute(join(out, "entry.mjs"), calls), trace);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + proof.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_future_calls' does not depend on any axioms/);
  const logic = join(out, "logic.mjs");
  await writeFile(logic, (await readFile(logic, "utf8")).replace("total *= factor", "total += factor"));
  assert.equal(cli("validate", source, spec, out).result, "unknown");
  assert.notDeepEqual(execute(join(out, "entry.mjs"), calls), trace);
}

if (shard === 11) {
  await writeFile(
    source,
    `let seed = 1;
let amount = 1;
function bump() { seed += amount; }
function make(start) { let value = start * seed; return (step) => { value += step; return value; }; }
bump();
const first = make(2);
export { first };
`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1, 2, 3] },
        "closures.mjs": { imports: ["state.mjs"], statements: [5] },
        "entry.mjs": { imports: ["state.mjs", "closures.mjs"], statements: [4] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).result, "generated");
  const witness = cli("validate", source, spec, out);
  assert.equal(witness.result, "unsafe", JSON.stringify(witness));
  assert.deepEqual(witness.calls, [{ name: "first", args: [0] }]);
  assert.deepEqual(execute(source, witness.calls).results, [{ value: 4 }]);
  assert.deepEqual(execute(join(out, "entry.mjs"), witness.calls).results, [{ value: 2 }]);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + witness.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_witness_difference' does not depend on any axioms/);
}

if (shard === 12) {
  await writeFile(
    source,
    `let later = 9;
function choose(flag) { if (flag) return 3; let local = 4; return local + later; }
function check(flag) { if (flag) throw "bad"; return later; }
export { choose, check };
`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0] },
        "logic.mjs": { imports: ["state.mjs"], statements: [1, 2] },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).result, "generated");
  const proof = cli("validate", source, spec, out);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = [
    { name: "choose", args: [0] },
    { name: "choose", args: [1] },
    { name: "check", args: [0] },
    { name: "check", args: [1] },
  ];
  const trace = execute(source, calls);
  assert.deepEqual(trace.results, [{ value: 13 }, { value: 3 }, { value: 9 }]);
  assert.deepEqual(trace.error, { name: null, phase: "call", index: 3, thrown: { kind: "string", value: "bad" } });
  assert.deepEqual(execute(join(out, "entry.mjs"), calls), trace);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + proof.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_future_calls' does not depend on any axioms/);
  const tdzSource = join(root, "guard-tdz.mjs");
  const tdzSpec = join(root, "guard-tdz.json");
  const tdzOut = join(root, "guard-tdz-out");
  await writeFile(
    tdzSource,
    `let value = 9;
function broken(flag) { if (flag) return value; if (flag) return 0; let value = 1; return value; }
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
  assert.equal(execute(tdzSource, [{ name: "broken", args: [1] }]).error.name, "ReferenceError");
}

if (shard === 13) {
  await writeFile(
    source,
    `let seed = 1;
let amount = 1;
function bump() { seed += amount; }
function make(start) { let value = start * seed; return (step) => { if (step) return value; value += 1; return value; }; }
bump();
const first = make(2);
export { first };
`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1, 2, 3] },
        "closures.mjs": { imports: ["state.mjs"], statements: [5] },
        "entry.mjs": { imports: ["state.mjs", "closures.mjs"], statements: [4] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).result, "generated");
  const witness = cli("validate", source, spec, out);
  assert.equal(witness.result, "unsafe", JSON.stringify(witness));
  assert.deepEqual(witness.calls, [{ name: "first", args: [0] }]);
  assert.deepEqual(execute(source, witness.calls).results, [{ value: 5 }]);
  assert.deepEqual(execute(join(out, "entry.mjs"), witness.calls).results, [{ value: 3 }]);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + witness.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_witness_difference' does not depend on any axioms/);
}

if (shard === 14) {
  await writeFile(
    source,
    `function maybe(value) { if (value) throw 7; return value; }
function select(x, y) { if (x < y && y === 4) return 9; return 1; }
function short(x) { if (x === 0 || maybe(x)) return 3; return 4; }
function invert(x) { if (!x) return 8; return 2; }
function conjunction(x) { if (x && maybe(1)) return 1; return 2; }
export { select, short, invert, conjunction };
`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0] },
        "logic.mjs": { imports: ["state.mjs"], statements: [1, 2, 3, 4] },
        "entry.mjs": { imports: ["logic.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).result, "generated");
  const proof = cli("validate", source, spec, out);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = [
    { name: "select", args: [2, 4] },
    { name: "select", args: [5, 4] },
    { name: "select", args: [2, 3] },
    { name: "invert", args: [0] },
    { name: "invert", args: [2] },
    { name: "short", args: [0] },
    { name: "short", args: [1] },
  ];
  const trace = execute(source, calls);
  assert.deepEqual(trace.results, [{ value: 9 }, { value: 1 }, { value: 1 }, { value: 8 }, { value: 2 }, { value: 3 }]);
  assert.deepEqual(trace.error, { name: null, phase: "call", index: 6, thrown: { kind: "number", value: 7 } });
  assert.deepEqual(execute(join(out, "entry.mjs"), calls), trace);
  const conjunctionCalls = [
    { name: "conjunction", args: [0] },
    { name: "conjunction", args: [1] },
  ];
  const conjunctionTrace = execute(source, conjunctionCalls);
  assert.deepEqual(conjunctionTrace.results, [{ value: 2 }]);
  assert.deepEqual(conjunctionTrace.error, {
    name: null,
    phase: "call",
    index: 1,
    thrown: { kind: "number", value: 7 },
  });
  assert.deepEqual(execute(join(out, "entry.mjs"), conjunctionCalls), conjunctionTrace);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + proof.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_future_calls' does not depend on any axioms/);
  const logic = join(out, "logic.mjs");
  await writeFile(logic, (await readFile(logic, "utf8")).replace("x < y", "x === y"));
  assert.equal(cli("validate", source, spec, out).result, "unknown");
  assert.notDeepEqual(execute(join(out, "entry.mjs"), calls), trace);
  const booleanSource = join(root, "boolean-return.mjs");
  const booleanSpec = join(root, "boolean-return.json");
  await writeFile(booleanSource, "function compare(x) { return x === 1; } export { compare };\n");
  await writeFile(
    booleanSpec,
    JSON.stringify({ kind: "bindings", entry: "entry.mjs", modules: { "entry.mjs": { imports: [], statements: [0] } } })
  );
  const booleanOut = join(root, "boolean-return-out");
  assert.equal(cli("generate", booleanSource, booleanSpec, booleanOut).result, "generated");
  assert.equal(cli("validate", booleanSource, booleanSpec, booleanOut).result, "unknown");
  assert.deepEqual(execute(booleanSource, [{ name: "compare", args: [1] }]).results, [{ value: true }]);
  const tdzSource = join(root, "comparison-tdz.mjs");
  const tdzSpec = join(root, "comparison-tdz.json");
  const tdzOut = join(root, "comparison-tdz-out");
  await writeFile(
    tdzSource,
    `let value = 2;
function broken(flag) { if (value < flag) return 1; let value = 3; return value; }
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
  assert.equal(execute(tdzSource, [{ name: "broken", args: [1] }]).error.name, "ReferenceError");
}

if (shard === 15) {
  await writeFile(
    source,
    `let seed = 1;
let amount = 1;
function bump() { seed += amount; }
function make(start) { let value = start * seed; return (step) => { if (step < value) return value; return step; }; }
bump();
const first = make(2);
export { first };
`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1, 2, 3] },
        "closures.mjs": { imports: ["state.mjs"], statements: [5] },
        "entry.mjs": { imports: ["state.mjs", "closures.mjs"], statements: [4] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).result, "generated");
  const witness = cli("validate", source, spec, out);
  assert.equal(witness.result, "unsafe", JSON.stringify(witness));
  assert.deepEqual(witness.calls, [{ name: "first", args: [0] }]);
  assert.deepEqual(execute(source, witness.calls).results, [{ value: 4 }]);
  assert.deepEqual(execute(join(out, "entry.mjs"), witness.calls).results, [{ value: 2 }]);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + witness.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_witness_difference' does not depend on any axioms/);
}
