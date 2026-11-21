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
if (shard === 0) {
  const modelCheck = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input:
      models +
      "\n#print axioms captured_pairs_sequence_simulates\n#print axioms captured_pairs_runs_complete_together\n#print axioms captured_pairs_argument_calls_observations\n" +
      "\n#print axioms capture_matching_transfer\n#print axioms distinct_writeback_transfer\n#print axioms distinct_writebacks_agree\n#print axioms two_distinct_captured_call_sequence_simulates\n#print axioms decideCaptureKeysUnique\n" +
      "\n#print axioms runExecution_relation_preserved\n#print axioms executeCaptured_success\n#print axioms captured_paired_step_preserved\n#print axioms executeCaptured_decompose\n#print axioms captured_unblocked_step_related\n#print axioms captured_call_sequence_related\n#print axioms lookup_update_other\n#print axioms lookup_store_other\n#print axioms load_store_disjoint\n#print axioms storeCapturedFrame_origin\n#print axioms captured_step_preserves_disjoint\n#print axioms captured_step_preserves_watched\n#print axioms two_captured_call_sequence_related\n#print axioms successful_store_captures_matches\n#print axioms distinct_related_writebacks_exist\n#print axioms distinct_captured_writeback_exists\n#print axioms store_captures_roundtrip\n#print axioms load_captures_update_other\n#print axioms update_existing_cell\n#print axioms two_captured_call_sequence_simulates\n#print axioms runExecution_simulates\n#print axioms captured_step_simulates\n#print axioms paired_captured_table_call\n#print axioms two_captured_argument_calls_related\n#print axioms related_argument_call_operations\n#print axioms decideRelatedCapturedEnvironments\n#print axioms decideCapturesDisjoint\n" +
      'example : relatedCapturedEnvironments [("a", 3)] [("b", 3)] [("x", "a")] [("x", "b")] := by decide\n' +
      'example : ¬ relatedCapturedEnvironments [("a", 3)] [("b", 4)] [("x", "a")] [("x", "b")] := by decide\n' +
      'example : ¬ relatedCapturedEnvironments [] [] [("x", "a")] [("x", "b")] := by decide\n' +
      'theorem single_capture_writeback_example : ∃ result, storeCapturedFrame [("a", 1)] [("x", "a")] [("x", 3)] = some result := by\n' +
      '  apply distinct_captured_writeback_exists [("x", "a")] [("x", 3)] [("x", 1)] [("a", 1)]\n' +
      '  · exact .cons "x" "a" [] (by intro n k h; cases h) .nil\n' +
      '  · exact .cons "x" "a" 3 (by decide) .nil\n' +
      "  · decide\n#print axioms single_capture_writeback_example\n" +
      'example : capturesDisjoint [("x", "a"), ("y", "b")] [("z", "c")] := by decide\n' +
      'example : ¬ capturesDisjoint [("x", "a"), ("y", "b")] [("z", "b")] := by decide\n' +
      'example : CaptureKeysUnique [("x", "a"), ("y", "b")] := by decide\n' +
      'example : ¬ CaptureKeysUnique [("x", "a"), ("y", "a")] := by decide\n' +
      'example : capturedWritebacksAgree [("a", 0), ("b", 1)] [("c", 0), ("d", 1)] [("x", "a"), ("y", "b")] [("x", "c"), ("y", "d")] := by apply distinct_writebacks_agree <;> decide\n' +
      "theorem renamed_capture_observations (calls : List (String × List Nat)) :\n" +
      '  (runArgumentCalls [("f", .captured [("x", "a")] [] (.name "x")), ("g", .captured [("y", "b")] [] (.name "y"))] (⟨[], [], [], none, [], none, [("a", 1), ("b", 2)]⟩ : ExecutionState Unit) calls).map capturedCallObservation =\n' +
      '  (runArgumentCalls [("f", .captured [("x", "c")] [] (.name "x")), ("g", .captured [("y", "d")] [] (.name "y"))] (⟨[], [], [], none, [], none, [("c", 1), ("d", 2)]⟩ : ExecutionState Unit) calls).map capturedCallObservation := by\n' +
      '  apply two_distinct_captured_argument_calls_observations [("x", "a")] [("x", "c")] [("y", "b")] [("y", "d")] <;> try decide\n' +
      '  exact .cons "f" (.first _ _) (.cons "g" (.second _ _) .nil)\n#print axioms renamed_capture_observations\n' +
      'example : relatedCapturedEnvironments [("a", 0)] [("b", 0), ("c", 0)] [("x", "a"), ("y", "a")] [("x", "b"), ("y", "c")] := by decide\n' +
      'example : capturedStatesRelated [("x", "a"), ("y", "a")] [("x", "b"), ("y", "c")] (⟨[], [], [], none, [], none, [("a", 0)]⟩ : ExecutionState Unit) (⟨[], [], [], none, [], none, [("b", 0), ("c", 0)]⟩ : ExecutionState Unit) := by decide\n' +
      'example : (storeCapturedFrame [("a", 0)] [("x", "a"), ("y", "a")] [("x", 1), ("y", 0)]).isSome = false := by decide\n' +
      'example : (storeCapturedFrame [("b", 0), ("c", 0)] [("x", "b"), ("y", "c")] [("x", 1), ("y", 0)]).isSome = true := by decide\n' +
      'example : ¬ capturedWritebacksAgree [("a", 0)] [("b", 0), ("c", 0)] [("x", "a"), ("y", "a")] [("x", "b"), ("y", "c")] := by intro agree; have h := agree [("x", 1), ("y", 0)]; cases h\n' +
      'example : (executeStep (⟨[], [], [], none, [], none, [("a", 0)]⟩ : ExecutionState Unit) (.invokeCaptured [("x", "a"), ("y", "a")] [] [] (.assign "x" (.literal 1) (.literal 0)))).isSome = false := by decide\n' +
      'example : (executeStep (⟨[], [], [], none, [], none, [("b", 0), ("c", 0)]⟩ : ExecutionState Unit) (.invokeCaptured [("x", "b"), ("y", "c")] [] [] (.assign "x" (.literal 1) (.literal 0)))).isSome = true := by decide\n' +
      'example : argumentCallOperationLists [("f", .captured [("x", "cell")] ["n"] (.name "n"))] [("f", [2]), ("f", [3])] = some [[.invokeCaptured [("x", "cell")] ["n"] [2] (.name "n")], [.invokeCaptured [("x", "cell")] ["n"] [3] (.name "n")]] := by decide\n' +
      'example : argumentCallOperationLists [("f", .captured [] ["n"] (.name "n"))] [("missing", [2])] = none := by decide\n' +
      'example : argumentCallOperationLists [("f", .captured [] ["n"] (.name "n"))] [("f", [])] = none := by decide\n' +
      'example : PairedCapturedTables [("x", "a")] [("x", "b")] [("x", "c")] [("x", "d")] [("f", .captured [("x", "a")] ["n"] (.name "n")), ("g", .captured [("x", "c")] [] (.name "x"))] [("f", .captured [("x", "b")] ["n"] (.name "n")), ("g", .captured [("x", "d")] [] (.name "x"))] := by exact .cons "f" (.first _ _) (.cons "g" (.second _ _) .nil)\n',
  });
  assert.equal(modelCheck.status, 0, modelCheck.stdout + modelCheck.stderr);
  for (const name of [
    "captured_pairs_sequence_simulates",
    "captured_pairs_runs_complete_together",
    "captured_pairs_argument_calls_observations",
  ])
    assert.match(modelCheck.stdout, new RegExp(`'${name}' does not depend on any axioms`));
  assert.match(modelCheck.stdout, /'renamed_capture_observations' does not depend on any axioms/);
  for (const theorem of [
    "capture_matching_transfer",
    "distinct_writeback_transfer",
    "distinct_writebacks_agree",
    "two_distinct_captured_call_sequence_simulates",
    "decideCaptureKeysUnique",
  ])
    assert.match(modelCheck.stdout, new RegExp(`'${theorem}' does not depend on any axioms`));
  assert.match(modelCheck.stdout, /'runExecution_relation_preserved' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'executeCaptured_success' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'captured_paired_step_preserved' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'executeCaptured_decompose' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'captured_unblocked_step_related' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'captured_call_sequence_related' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'lookup_update_other' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'lookup_store_other' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'load_store_disjoint' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'storeCapturedFrame_origin' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'captured_step_preserves_disjoint' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'captured_step_preserves_watched' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'two_captured_call_sequence_related' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'single_capture_writeback_example' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'successful_store_captures_matches' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'distinct_related_writebacks_exist' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'distinct_captured_writeback_exists' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'store_captures_roundtrip' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'load_captures_update_other' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'update_existing_cell' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'two_captured_call_sequence_simulates' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'runExecution_simulates' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'captured_step_simulates' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'paired_captured_table_call' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'two_captured_argument_calls_related' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'related_argument_call_operations' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'decideRelatedCapturedEnvironments' does not depend on any axioms/);
  assert.match(modelCheck.stdout, /'decideCapturesDisjoint' does not depend on any axioms/);
}
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
  assert.equal(cli("validate", parameterSource, parameterSpec, parameterOut).result, "unsafe");
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
  assert.equal(cli("validate", callSource, callSpec, callOut).result, "unsafe");
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
function make(start) { let value = start + seed; function increment(step) { value += step; return value; } return increment; }
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
function make(start) { let value = start + seed; function increment(step) { value += step; return value; } return increment; }
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

if (shard === 15) {
  const finallySource = join(root, "finally-source.mjs");
  const finallySpec = join(root, "finally-spec.json");
  const finallyOut = join(root, "finally-out");
  await writeFile(
    finallySource,
    `function fail(n) { if (n) throw 8; return n; }
function make(start) { let value = start; return (step) => { try { if (step) throw 7; return value; } finally { value += 1; value += fail(step); } }; }
const counter = make(2);
export { counter };
`
  );
  await writeFile(
    finallySpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1, 2] },
        "entry.mjs": { imports: ["state.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", finallySource, finallySpec, finallyOut).result, "generated");
  const proof = cli("validate", finallySource, finallySpec, finallyOut);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = [
    { name: "counter", args: [0] },
    { name: "counter", args: [0] },
    { name: "counter", args: [1] },
  ];
  const trace = execute(finallySource, calls);
  assert.deepEqual(trace.results, [{ value: 2 }, { value: 3 }]);
  assert.deepEqual(trace.error, { name: null, phase: "call", index: 2, thrown: { kind: "number", value: 8 } });
  assert.deepEqual(execute(join(finallyOut, "entry.mjs"), calls), trace);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input:
      models +
      proof.certificate +
      `
theorem finally_modeled_observation :
  ((sourceBindingState bindingSource).bind (fun state =>
    (publicCallableTable bindingSource bindingExports).bind (fun table =>
      runArgumentCalls table state [("counter", [0]), ("counter", [0]), ("counter", [1])]))).map
    (fun state => (state.returns, state.thrown)) = some ([some 2, some 3], some (.number 8)) := by decide
#print axioms finally_modeled_observation
`,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_future_calls' does not depend on any axioms/);
  assert.match(replay.stdout, /'finally_modeled_observation' does not depend on any axioms/);
  assert.match(replay.stdout, /'binding_captured_observations' does not depend on any axioms/);
  const unsupportedSource = join(root, "finally-return.mjs");
  await writeFile(
    unsupportedSource,
    (await readFile(finallySource, "utf8")).replace("value += 1; value += fail(step);", "return 5;")
  );
  assert.equal(cli("generate", unsupportedSource, finallySpec, join(root, "finally-return-out")).result, "unknown");
}

if (shard === 15) {
  const source = join(root, "catch-source.mjs");
  const spec = join(root, "catch-spec.json");
  const out = join(root, "catch-out");
  await writeFile(
    source,
    `function make(start) { let value = start; return (step) => { try { if (step) throw 7; return value; } catch { value += 2; return value; } finally { value += 1; } }; }
const counter = make(2);
export { counter };
`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1] },
        "entry.mjs": { imports: ["state.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).result, "generated");
  const proof = cli("validate", source, spec, out);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = [0, 1, 0].map((step) => ({ name: "counter", args: [step] }));
  const trace = execute(source, calls);
  assert.deepEqual(trace.results, [{ value: 2 }, { value: 5 }, { value: 6 }]);
  assert.deepEqual(execute(join(out, "entry.mjs"), calls), trace);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + proof.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_captured_observations' does not depend on any axioms/);
  const emitted = join(out, "state.mjs");
  const originalOutput = await readFile(emitted, "utf8");
  assert.ok(originalOutput.includes("value += 2"));
  await writeFile(emitted, originalOutput.replace("value += 2", "value += 3"));
  const witness = cli("validate", source, spec, out);
  assert.equal(witness.result, "unsafe", JSON.stringify(witness));
  assert.notDeepEqual(execute(source, witness.calls), execute(join(out, "entry.mjs"), witness.calls));
  const witnessReplay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + witness.certificate,
  });
  assert.equal(witnessReplay.status, 0, witnessReplay.stdout + witnessReplay.stderr);
  assert.match(witnessReplay.stdout, /'binding_witness_difference' does not depend on any axioms/);
  const bound = join(root, "catch-binding.mjs");
  const boundOut = join(root, "catch-binding-out");
  await writeFile(bound, (await readFile(source, "utf8")).replace("catch {", "catch (e) {"));
  assert.equal(cli("generate", bound, spec, boundOut).result, "generated");
  assert.equal(cli("validate", bound, spec, boundOut).result, "proved under assumptions");
  assert.deepEqual(execute(bound, calls), execute(join(boundOut, "entry.mjs"), calls));
  const used = join(root, "catch-binding-used.mjs");
  await writeFile(used, (await readFile(bound, "utf8")).replace("value += 2", "value += e"));
  assert.equal(cli("generate", used, spec, join(root, "catch-binding-used-out")).result, "unknown");
}

if (shard === 13) {
  const source = join(root, "bit-and.mjs");
  const spec = join(root, "bit-and.json");
  const out = join(root, "bit-and-out");
  await writeFile(
    source,
    `function mask(x) { return x & 3; }
function maskAssign(x) { let value = x; value &= 3; return value; }
function combine(x) { return x | 3; }
function toggle(x) { return x ^ 3; }
function combineAssign(x) { let value = x; value |= 3; return value; }
function toggleAssign(x) { let value = x; value ^= 3; return value; }
export { mask, maskAssign, combine, toggle, combineAssign, toggleAssign };
`
  );
  await writeFile(
    spec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "functions.mjs": { imports: [], statements: [0, 1, 2, 3, 4, 5] },
        "entry.mjs": { imports: ["functions.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", source, spec, out).result, "generated");
  const proof = cli("validate", source, spec, out);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  assert.ok(proof.assumptions.some((assumption) => assumption.includes("bitwise results are nonnegative")));
  const calls = [
    { name: "mask", args: [4294967297] },
    { name: "mask", args: [Number.MAX_SAFE_INTEGER] },
    { name: "maskAssign", args: [4294967298] },
    { name: "combine", args: [4294967296] },
    { name: "toggle", args: [4294967297] },
    { name: "combineAssign", args: [4] },
    { name: "toggleAssign", args: [4] },
  ];
  const trace = execute(source, calls);
  assert.deepEqual(trace.results, [
    { value: 1 },
    { value: 3 },
    { value: 2 },
    { value: 3 },
    { value: 2 },
    { value: 7 },
    { value: 7 },
  ]);
  assert.deepEqual(execute(join(out, "entry.mjs"), calls), trace);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input:
      models +
      proof.certificate +
      `
theorem bit_and_observation :
  ((sourceBindingState bindingSource).bind (fun state =>
    (publicCallableTable bindingSource bindingExports).bind (fun table =>
      runArgumentCalls table state [("mask", [4294967297]), ("mask", [9007199254740991]), ("maskAssign", [4294967298]), ("combine", [4294967296]), ("toggle", [4294967297]), ("combineAssign", [4]), ("toggleAssign", [4])]))).map
    (fun state => state.returns) = some [some 1, some 3, some 2, some 3, some 2, some 7, some 7] := by decide
#print axioms bit_and_observation
#print axioms bitwiseFuel
example : evaluateExpression [] [] (.bitAnd (.literal 2147483648) (.literal 2147483648)) = none := by decide
example : evaluateExpression [] [] (.bitOr (.literal 2147483648) (.literal 0)) = none := by decide
example : evaluateExpression [] [] (.bitXor (.literal 2147483648) (.literal 0)) = none := by decide
`,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'bit_and_observation' does not depend on any axioms/);
  assert.match(replay.stdout, /'bitwiseFuel' does not depend on any axioms/);
  const emitted = join(out, "functions.mjs");
  const original = await readFile(emitted, "utf8");
  for (const operator of ["&", "|", "^"]) {
    await writeFile(emitted, original.replace(`x ${operator} 3`, `x ${operator} 1`));
    const unsafe = cli("validate", source, spec, out);
    assert.equal(unsafe.result, "unsafe", JSON.stringify(unsafe));
    assert.notDeepEqual(execute(source, unsafe.calls), execute(join(out, "entry.mjs"), unsafe.calls));
    const invalid = join(root, `bit-negative-${operator.charCodeAt(0)}.mjs`);
    await writeFile(
      invalid,
      (await readFile(source, "utf8")).replace(
        `x ${operator} 3`,
        `2147483648 ${operator} ${operator === "&" ? 2147483648 : 0}`
      )
    );
    const invalidOut = join(root, `bit-negative-${operator.charCodeAt(0)}-out`);
    const generated = cli("generate", invalid, spec, invalidOut);
    if (generated.result === "generated") assert.equal(cli("validate", invalid, spec, invalidOut).result, "unknown");
    else assert.equal(generated.result, "unknown");
  }
}

if (shard === 14) {
  const logicalSource = join(root, "logical-values.mjs");
  const logicalSpec = join(root, "logical-values.json");
  const logicalOut = join(root, "logical-values-out");
  await writeFile(
    logicalSource,
    `function boom(x) { if (x) throw 7; return x; }
function count(x) { let n = x; function next() { n += 1; return n; } return next() || 9; }
function and(x) { return x && boom(1); }
function or(x) { return x || boom(1); }
function nested(x) { return (x || 2) || 3; }
export { count, and, or, nested };
`
  );
  await writeFile(
    logicalSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "helpers.mjs": { imports: [], statements: [0] },
        "functions.mjs": { imports: ["helpers.mjs"], statements: [1, 2, 3, 4] },
        "entry.mjs": { imports: ["functions.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", logicalSource, logicalSpec, logicalOut).result, "generated");
  const proof = cli("validate", logicalSource, logicalSpec, logicalOut);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = [
    { name: "count", args: [0] },
    { name: "and", args: [0] },
    { name: "or", args: [2] },
    { name: "nested", args: [0] },
    { name: "nested", args: [5] },
  ];
  const trace = execute(logicalSource, calls);
  assert.deepEqual(trace.results, [{ value: 1 }, { value: 0 }, { value: 2 }, { value: 2 }, { value: 5 }]);
  assert.deepEqual(execute(join(logicalOut, "entry.mjs"), calls), trace);
  for (const call of [
    { name: "and", args: [1] },
    { name: "or", args: [0] },
  ]) {
    const thrown = execute(logicalSource, [call]);
    assert.deepEqual(thrown.error?.thrown, { kind: "number", value: 7 });
    assert.deepEqual(execute(join(logicalOut, "entry.mjs"), [call]), thrown);
  }
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input:
      models +
      proof.certificate +
      `
theorem logical_values_observation :
  ((sourceBindingState bindingSource).bind (fun state =>
    (publicCallableTable bindingSource bindingExports).bind (fun table =>
      runArgumentCalls table state [("count", [0]), ("and", [0]), ("or", [2]), ("nested", [0]), ("nested", [5])]))).map
    (fun state => state.returns) = some [some 1, some 0, some 2, some 2, some 5] := by decide
#print axioms logical_values_observation
theorem logical_throw_observation :
  ((sourceBindingState bindingSource).bind (fun state =>
    (publicCallableTable bindingSource bindingExports).bind (fun table =>
      runArgumentCalls table state [("or", [0])]))).map
    (fun state => state.thrown) = some (some (.number 7)) := by decide
#print axioms logical_throw_observation
`,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'logical_values_observation' does not depend on any axioms/);
  assert.match(replay.stdout, /'logical_throw_observation' does not depend on any axioms/);
}

if (shard === 15) {
  const loopSource = join(root, "loop-source.mjs");
  const loopSpec = join(root, "loop-spec.json");
  const loopOut = join(root, "loop-out");
  await writeFile(
    loopSource,
    `function maybe(n) { if (n === 2) throw 9; return n; }
function sum(x) { let total = x; let i = 5; for (let i = 0; i < 3; i++) { total += i; } return total + i; }
function zero(x) { let total = x; for (let i = 0; i < 0; i++) { total += 1; } return total; }
function interrupted(x) { let total = x; for (let i = 0; i < 4; i++) { total += maybe(i); } return total; }
function inclusive(x) { let total = x; for (let i = 2; i <= 4; i += 1) { total += i; } return total; }
function reversed(x) { let total = x; for (let i = 4; i < 2; i++) { total += i; } return total; }
function recover(x) { let total = 1; try { total += maybe(x); } catch (error) { total += 5; } finally { total += 2; } return total; }
function direct(x) { let total = x; try { total += 1; throw 5; } catch { total += 2; } return total; }
function remainder(x) { return x % 3; }
function remainderAssign(x) { let value = x; value %= 3; return value; }
function divide(x) { return x / 2; }
function divideAssign(x) { let value = x; value /= 2; return value; }
function looseEqual(x) { if (x == 2) return 1; return 0; }
function looseNotEqual(x) { if (x != 2) return 1; return 0; }
function ternary(x) { return x ? maybe(x) : 4; }
function shortCircuit(x) { return x ? 4 : maybe(2); }
export { sum, zero, interrupted, inclusive, reversed, recover, direct, remainder, remainderAssign, divide, divideAssign, looseEqual, looseNotEqual, ternary, shortCircuit };
`
  );
  await writeFile(
    loopSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "helpers.mjs": { imports: [], statements: [0] },
        "functions.mjs": { imports: ["helpers.mjs"], statements: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15] },
        "entry.mjs": { imports: ["functions.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", loopSource, loopSpec, loopOut).result, "generated");
  const proof = cli("validate", loopSource, loopSpec, loopOut);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  assert.ok(proof.assumptions.some((assumption) => assumption.includes("negative zero")));
  assert.ok(proof.assumptions.some((assumption) => assumption.includes("remainder and division divisors are nonzero")));
  assert.ok(proof.assumptions.some((assumption) => assumption.includes("division results are integers")));
  const calls = [
    { name: "sum", args: [2] },
    { name: "zero", args: [4] },
    { name: "inclusive", args: [1] },
    { name: "reversed", args: [6] },
    { name: "recover", args: [0] },
    { name: "recover", args: [2] },
    { name: "direct", args: [4] },
    { name: "remainder", args: [8] },
    { name: "remainderAssign", args: [7] },
    { name: "divide", args: [8] },
    { name: "divideAssign", args: [6] },
    { name: "looseEqual", args: [2] },
    { name: "looseEqual", args: [3] },
    { name: "looseNotEqual", args: [2] },
    { name: "looseNotEqual", args: [3] },
    { name: "ternary", args: [0] },
    { name: "ternary", args: [1] },
    { name: "shortCircuit", args: [1] },
    { name: "interrupted", args: [1] },
  ];
  const trace = execute(loopSource, calls);
  assert.deepEqual(trace.results, [
    { value: 10 },
    { value: 4 },
    { value: 10 },
    { value: 6 },
    { value: 3 },
    { value: 8 },
    { value: 7 },
    { value: 2 },
    { value: 1 },
    { value: 4 },
    { value: 3 },
    { value: 1 },
    { value: 0 },
    { value: 0 },
    { value: 1 },
    { value: 4 },
    { value: 1 },
    { value: 4 },
  ]);
  assert.deepEqual(trace.error, { name: null, phase: "call", index: 18, thrown: { kind: "number", value: 9 } });
  assert.deepEqual(execute(join(loopOut, "entry.mjs"), calls), trace);
  const thrownTernary = [{ name: "shortCircuit", args: [0] }];
  assert.deepEqual(execute(loopSource, thrownTernary).error?.thrown, { kind: "number", value: 9 });
  assert.deepEqual(execute(join(loopOut, "entry.mjs"), thrownTernary), execute(loopSource, thrownTernary));
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input:
      models +
      proof.certificate +
      `
theorem loop_modeled_observation :
  ((sourceBindingState bindingSource).bind (fun state =>
    (publicCallableTable bindingSource bindingExports).bind (fun table =>
      runArgumentCalls table state [("sum", [2]), ("zero", [4]), ("inclusive", [1]), ("reversed", [6]), ("recover", [0]), ("recover", [2]), ("direct", [4]), ("remainder", [8]), ("remainderAssign", [7]), ("divide", [8]), ("divideAssign", [6]), ("looseEqual", [2]), ("looseEqual", [3]), ("looseNotEqual", [2]), ("looseNotEqual", [3]), ("ternary", [0]), ("ternary", [1]), ("shortCircuit", [1]), ("interrupted", [1])]))).map
    (fun state => (state.returns, state.thrown)) = some ([some 10, some 4, some 10, some 6, some 3, some 8, some 7, some 2, some 1, some 4, some 3, some 1, some 0, some 0, some 1, some 4, some 1, some 4], some (.number 9)) := by decide
#print axioms loop_modeled_observation
theorem ternary_throw_observation :
  ((sourceBindingState bindingSource).bind (fun state =>
    (publicCallableTable bindingSource bindingExports).bind (fun table =>
      runArgumentCalls table state [("shortCircuit", [0])]))).map
    (fun state => state.thrown) = some (some (.number 9)) := by decide
#print axioms ternary_throw_observation
example : evaluateExpression [] [] (.remainder (.literal 4) (.literal 0)) = none := by decide
example : evaluateExpression [] [] (.divide (.literal 4) (.literal 0)) = none := by decide
example : evaluateExpression [] [] (.divide (.literal 5) (.literal 2)) = none := by decide
`,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'loop_modeled_observation' does not depend on any axioms/);
  assert.match(replay.stdout, /'ternary_throw_observation' does not depend on any axioms/);
  const emitted = join(loopOut, "functions.mjs");
  const originalOutput = await readFile(emitted, "utf8");
  assert.ok(originalOutput.includes("x / 2"));
  await writeFile(emitted, originalOutput.replace("x / 2", "x / 1"));
  const changed = cli("validate", loopSource, loopSpec, loopOut);
  assert.equal(changed.result, "unsafe", JSON.stringify(changed));
  assert.notDeepEqual(execute(loopSource, changed.calls), execute(join(loopOut, "entry.mjs"), changed.calls));
  await writeFile(emitted, originalOutput);
  const unsupported = join(root, "dynamic-loop.mjs");
  await writeFile(unsupported, (await readFile(loopSource, "utf8")).replace("i < 3", "i < x"));
  assert.equal(cli("generate", unsupported, loopSpec, join(root, "dynamic-loop-out")).result, "unknown");
  const tooMany = join(root, "too-many-loop.mjs");
  await writeFile(tooMany, (await readFile(loopSource, "utf8")).replace("i < 3", "i <= 8"));
  assert.equal(cli("generate", tooMany, loopSpec, join(root, "too-many-loop-out")).result, "unknown");
  for (const [name, from, to] of [
    ["remainder-zero", "x % 3", "x % 0"],
    ["remainder-assign-zero", "value %= 3", "value %= 0"],
    ["divide-zero", "x / 2", "x / 0"],
    ["divide-fraction", "x / 2", "5 / 2"],
    ["divide-assign-zero", "value /= 2", "value /= 0"],
    ["loose-coercion", "x == 2", "x == '2'"],
  ]) {
    const invalid = join(root, `${name}.mjs`);
    const invalidOut = join(root, `${name}-out`);
    await writeFile(invalid, (await readFile(loopSource, "utf8")).replace(from, to));
    const generated = cli("generate", invalid, loopSpec, invalidOut);
    if (generated.result === "generated")
      assert.equal(cli("validate", invalid, loopSpec, invalidOut).result, "unknown");
    else assert.equal(generated.result, "unknown");
  }
}

if (shard === 10) {
  const dataSource = join(root, "local-data.mjs");
  const dataSpec = join(root, "local-data.json");
  const dataOut = join(root, "local-data-out");
  await writeFile(
    dataSource,
    `let bias = 1;
let amount = 2;
function bump() { bias += amount; }
function read(flag) { const box = { value: 3 }; const alias = box; const values = [2, 5]; if (flag) { alias.value += values[0]; } else { alias.value += values[1]; } if (flag) { alias.value += 1; } return box.value + bias; }
bump();
globalThis.record(bias);
export { read };
`
  );
  const specFor = (unsafe) => ({
    kind: "bindings",
    entry: "entry.mjs",
    modules: {
      "state.mjs": { imports: [], statements: [0, 1, 2] },
      "read.mjs": { imports: ["state.mjs"], statements: unsafe ? [3, 5] : [3, 4, 5] },
      "entry.mjs": { imports: ["state.mjs", "read.mjs"], statements: unsafe ? [4] : [] },
    },
  });
  await writeFile(dataSpec, JSON.stringify(specFor(false)));
  assert.equal(cli("generate", dataSource, dataSpec, dataOut).result, "generated");
  const proof = cli("validate", dataSource, dataSpec, dataOut);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = [
    { name: "read", args: [0] },
    { name: "read", args: [1] },
  ];
  const observed = execute(dataSource, calls);
  assert.deepEqual(observed.results, [{ value: 11 }, { value: 9 }]);
  assert.deepEqual(execute(join(dataOut, "entry.mjs"), calls), observed);
  const emitted = join(dataOut, "read.mjs");
  const contents = await readFile(emitted, "utf8");
  const changed = contents.replace("value: 3", "value: 4");
  assert.notEqual(changed, contents);
  await writeFile(emitted, changed);
  assert.notEqual(cli("validate", dataSource, dataSpec, dataOut).result, "proved under assumptions");
  assert.notDeepEqual(execute(join(dataOut, "entry.mjs"), calls), observed);
  await writeFile(emitted, contents);
  const unsafeSpec = join(root, "local-data-unsafe.json"),
    unsafeOut = join(root, "local-data-unsafe-out");
  await writeFile(unsafeSpec, JSON.stringify(specFor(true)));
  assert.equal(cli("generate", dataSource, unsafeSpec, unsafeOut).result, "generated");
  const unsafe = cli("validate", dataSource, unsafeSpec, unsafeOut);
  assert.equal(unsafe.result, "unsafe", JSON.stringify(unsafe));
  assert.notDeepEqual(
    execute(dataSource, unsafe.calls ?? []),
    execute(join(unsafeOut, "entry.mjs"), unsafe.calls ?? [])
  );
  for (const [index, body] of [
    "const box = { value: 1 }; return box[flag];",
    "const box = { value: 1 }; return box;",
    "const box = { get value() { return 1; } }; return box.value;",
    "const box = { value: 1 }; box.missing = 2; return box.value;",
  ].entries()) {
    const unsupported = join(root, `local-data-unsupported-${index}.mjs`);
    await writeFile(unsupported, `function read(flag) { ${body} } export { read };\n`);
    const unsupportedSpec = join(root, `local-data-unsupported-${index}.json`);
    await writeFile(
      unsupportedSpec,
      JSON.stringify({
        kind: "bindings",
        entry: "entry.mjs",
        modules: {
          "entry.mjs": { imports: [], statements: [0] },
        },
      })
    );
    const unsupportedOut = join(root, `local-data-unsupported-${index}-out`);
    const generated = cli("generate", unsupported, unsupportedSpec, unsupportedOut);
    if (generated.result === "generated")
      assert.equal(cli("validate", unsupported, unsupportedSpec, unsupportedOut).result, "unknown");
    else assert.equal(generated.result, "unknown");
  }
}

if (shard === 8) {
  const manySource = join(root, "many-captures.mjs");
  const manySpec = join(root, "many-captures.json");
  const manyOut = join(root, "many-captures-out");
  await writeFile(
    manySource,
    `function make(start) { let value = start; return (step) => { value += step; return value; }; }
const first = make(2);
const second = make(5);
const third = make(10);
const alias = first;
export { first, second, third, alias };
`
  );
  await writeFile(
    manySpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "state.mjs": { imports: [], statements: [0, 1, 2, 3] },
        "aliases.mjs": { imports: ["state.mjs"], statements: [4] },
        "entry.mjs": { imports: ["state.mjs", "aliases.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", manySource, manySpec, manyOut).result, "generated");
  const proof = cli("validate", manySource, manySpec, manyOut);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  assert.match(proof.certificate, /binding_capture_pairs_unique/);
  const calls = [
    { name: "first", args: [1] },
    { name: "second", args: [2] },
    { name: "third", args: [3] },
    { name: "alias", args: [4] },
  ];
  const observed = execute(manySource, calls);
  assert.deepEqual(observed.results, [{ value: 3 }, { value: 7 }, { value: 13 }, { value: 7 }]);
  assert.deepEqual(execute(join(manyOut, "entry.mjs"), calls), observed);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input:
      models +
      proof.certificate +
      `
theorem many_distinct_capture_keys :
  (publicCapturedEnvironment bindingSource "first").getD [] ≠
  (publicCapturedEnvironment (namespacedOutputDefinitions binding_output_key_names bindingModules) "first").getD [] := by decide
#print axioms many_distinct_capture_keys
`,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_captured_observations' does not depend on any axioms/);
  assert.match(replay.stdout, /'many_distinct_capture_keys' does not depend on any axioms/);
  const aliases = join(manyOut, "aliases.mjs");
  await writeFile(aliases, (await readFile(aliases, "utf8")).replace("alias = first", "alias = second"));
  assert.notEqual(cli("validate", manySource, manySpec, manyOut).result, "proved under assumptions");
  assert.notDeepEqual(execute(join(manyOut, "entry.mjs"), calls), observed);
}

if (shard === 9) {
  const localSource = join(root, "local-functions.mjs");
  const localSpec = join(root, "local-functions.json");
  const localOut = join(root, "local-functions-out");
  await writeFile(
    localSource,
    `function calculate(start) { let current = start; function add(delta) { current += delta; return current; } let temporary = add(2); return add(temporary); }
function guarded(value) { function inner(flag) { if (flag) throw 9; return value; } return inner(value); }
export { calculate, guarded };
`
  );
  await writeFile(
    localSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "calculate.mjs": { imports: [], statements: [0] },
        "guarded.mjs": { imports: [], statements: [1] },
        "entry.mjs": { imports: ["calculate.mjs", "guarded.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", localSource, localSpec, localOut).result, "generated");
  const proof = cli("validate", localSource, localSpec, localOut);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = [
    { name: "calculate", args: [3] },
    { name: "guarded", args: [0] },
    { name: "guarded", args: [1] },
  ];
  const trace = execute(localSource, calls);
  assert.deepEqual(trace.results, [{ value: 10 }, { value: 0 }]);
  assert.deepEqual(trace.error, { name: null, phase: "call", index: 2, thrown: { kind: "number", value: 9 } });
  assert.deepEqual(execute(join(localOut, "entry.mjs"), calls), trace);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input:
      models +
      proof.certificate +
      `
theorem local_function_observation :
  ((sourceBindingState bindingSource).bind (fun state =>
    (publicCallableTable bindingSource bindingExports).bind (fun table =>
      runArgumentCalls table state [("calculate", [3]), ("guarded", [0]), ("guarded", [1])]))).map
    (fun state => (state.returns, state.thrown)) = some ([some 10, some 0], some (.number 9)) := by decide
#print axioms local_function_observation
`,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'local_function_observation' does not depend on any axioms/);
  const shadowed = join(root, "local-function-shadow.mjs");
  const shadowSpec = join(root, "local-function-shadow.json");
  await writeFile(
    shadowed,
    `function bad(x) { function inner() { return x; } if (x) { let x = 2; return inner(); } return 0; } export { bad };`
  );
  await writeFile(
    shadowSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "entry.mjs": { imports: [], statements: [0] },
      },
    })
  );
  const shadowOut = join(root, "local-function-shadow-out");
  const generated = cli("generate", shadowed, shadowSpec, shadowOut);
  if (generated.result === "generated")
    assert.equal(cli("validate", shadowed, shadowSpec, shadowOut).result, "unknown");
  else assert.equal(generated.result, "unknown");
  const shadowedCall = join(root, "local-function-shadowed-call.mjs");
  await writeFile(
    shadowedCall,
    `function bad(x) { function inner() { return 1; } if (x) { let inner = 2; return inner(); } return 0; } export { bad };`
  );
  const shadowedCallOut = join(root, "local-function-shadowed-call-out");
  const shadowedCallGenerated = cli("generate", shadowedCall, shadowSpec, shadowedCallOut);
  if (shadowedCallGenerated.result === "generated")
    assert.equal(cli("validate", shadowedCall, shadowSpec, shadowedCallOut).result, "unknown");
  else assert.equal(shadowedCallGenerated.result, "unknown");
  const constWrite = join(root, "local-function-const.mjs");
  await writeFile(
    constWrite,
    `function bad() { const x = 1; function inner() { x += 1; return x; } return inner(); } export { bad };`
  );
  const constOut = join(root, "local-function-const-out");
  const constGenerated = cli("generate", constWrite, shadowSpec, constOut);
  if (constGenerated.result === "generated")
    assert.equal(cli("validate", constWrite, shadowSpec, constOut).result, "unknown");
  else assert.equal(constGenerated.result, "unknown");
}

if (shard === 8) {
  const independentSource = join(root, "independent-named.mjs");
  const independentSpec = join(root, "independent-named.json");
  const independentOut = join(root, "independent-named-out");
  await writeFile(
    independentSource,
    `function make(start) {
  let value = start;
  function increment(delta) { value += delta; return value; }
  return increment;
}
const first = make(1);
const second = make(10);
export { first, second };
`
  );
  await writeFile(
    independentSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: {
        "factory.mjs": { imports: [], statements: [0] },
        "instances.mjs": { imports: ["factory.mjs"], statements: [1, 2] },
        "entry.mjs": { imports: ["instances.mjs"], statements: [] },
      },
    })
  );
  assert.equal(cli("generate", independentSource, independentSpec, independentOut).result, "generated");
  const independentProof = cli("validate", independentSource, independentSpec, independentOut);
  assert.equal(independentProof.result, "proved under assumptions", JSON.stringify(independentProof));
  const independentCalls = [
    { name: "first", args: [2] },
    { name: "second", args: [3] },
    { name: "first", args: [4] },
    { name: "second", args: [1] },
  ];
  const independentTrace = execute(independentSource, independentCalls);
  assert.deepEqual(independentTrace.results, [{ value: 3 }, { value: 13 }, { value: 7 }, { value: 14 }]);
  assert.deepEqual(execute(join(independentOut, "entry.mjs"), independentCalls), independentTrace);
  const independentReplay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input:
      models +
      independentProof.certificate +
      "\ntheorem real_split_capture_keys_differ : binding_source_first ≠ binding_output_first := by decide\n#print axioms real_split_capture_keys_differ\n",
  });
  assert.equal(independentReplay.status, 0, independentReplay.stdout + independentReplay.stderr);
  assert.match(independentReplay.stdout, /'binding_future_calls' does not depend on any axioms/);
  assert.match(independentReplay.stdout, /'binding_two_captured_calls' does not depend on any axioms/);
  assert.match(independentReplay.stdout, /'binding_two_captured_calls_simulate' does not depend on any axioms/);
  assert.match(
    independentReplay.stdout,
    /'binding_two_captured_calls_complete_together' does not depend on any axioms/
  );
  assert.doesNotMatch(
    independentProof.certificate.match(
      /theorem binding_two_captured_calls_simulate[\s\S]*?#print axioms binding_two_captured_calls_simulate/
    )[0],
    /\(writebacks\s*:/
  );
  assert.match(independentReplay.stdout, /'binding_exported_captured_calls' does not depend on any axioms/);
  assert.match(independentReplay.stdout, /'binding_exported_captured_calls_simulate' does not depend on any axioms/);
  assert.match(independentReplay.stdout, /'binding_captured_observations' does not depend on any axioms/);
  assert.match(independentReplay.stdout, /'real_split_capture_keys_differ' does not depend on any axioms/);
  await writeFile(
    source,
    `let seed = 1;
function make(start) { let value = start + seed; function increment(step) { value += step; return value; } return increment; }
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
    input:
      models +
      proof.certificate +
      `
example : relatedCapturedEnvironments [("left-cell", 3)] [("right-cell", 3)]
    [("value", "left-cell")] [("value", "right-cell")] := by
  exact ⟨[("value", 3)], rfl, rfl⟩
example : evaluateCapturedFrame [] [("left-cell", 3)] [("value", "left-cell")] [] []
    (.assign "value" (.add (.name "value") (.literal 2)) (.name "value")) =
      some ⟨[("value", 5)], .ok 5⟩ := by rfl
example : (storeCapturedFrame [("left-cell", 3)] [("value", "left-cell")]
    [("value", 5)]).map Subtype.val = some [("left-cell", 5)] := by decide
example : (storeCapturedFrame [("cell", 3)] [("first", "cell"), ("second", "cell")]
    [("first", 4), ("second", 5)]).map Subtype.val = none := by decide
#print axioms captured_frame_evaluation_preserved
#print axioms captured_frame_writeback_preserved
#print axioms captured_call_frame_preserved
#print axioms captured_completion_preserved
#print axioms captured_machine_step_preserved
#print axioms runExecution_relation_preserved
#print axioms executeCaptured_success
#print axioms captured_paired_step_preserved
def frameExampleBody : NumericExpr :=
  .assign "value" (.add (.name "value") (.add (.name "delta") (.name "seed"))) (.name "value")
def frameExampleLeft : ExecutionState Unit := ⟨[("seed", 4)], [], [], none, [], none, [("left-cell", 3)]⟩
def frameExampleRight : ExecutionState Unit := ⟨[("seed", 4)], [], [], none, [], none, [("right-cell", 3)]⟩
example : (executeStep frameExampleLeft (.invokeCaptured [("value", "left-cell")] ["delta"] [2] frameExampleBody)).map
    (fun state => (state.bindings, loadCaptures state.captured [("value", "left-cell")], state.returns)) =
    some ([("seed", 4)], some [("value", 9)], [some 9]) := by decide
example : (executeStep frameExampleRight (.invokeCaptured [("value", "right-cell")] ["delta"] [2] frameExampleBody)).map
    (fun state => (state.bindings, loadCaptures state.captured [("value", "right-cell")], state.returns)) =
    some ([("seed", 4)], some [("value", 9)], [some 9]) := by decide
def frameExampleLeftFinal : ExecutionState Unit :=
  ⟨[("seed", 4)], [], [], none, [some 9, some 14], none, [("left-cell", 14)]⟩
def frameExampleRightFinal : ExecutionState Unit :=
  ⟨[("seed", 4)], [], [], none, [some 9, some 14], none, [("right-cell", 14)]⟩
example : capturedStatesRelated [("value", "left-cell")] [("value", "right-cell")]
    frameExampleLeftFinal frameExampleRightFinal := by
  refine captured_call_sequence_related [("value", "left-cell")] [("value", "right-cell")]
    [.invokeCaptured [("value", "left-cell")] ["delta"] [2] frameExampleBody,
     .invokeCaptured [("value", "left-cell")] ["delta"] [1] frameExampleBody]
    [.invokeCaptured [("value", "right-cell")] ["delta"] [2] frameExampleBody,
     .invokeCaptured [("value", "right-cell")] ["delta"] [1] frameExampleBody]
    ?_ frameExampleLeft frameExampleRight frameExampleLeftFinal frameExampleRightFinal ?_ ?_ ?_
  · exact .cons (.call ["delta"] [2] frameExampleBody) (.cons (.call ["delta"] [1] frameExampleBody) .nil)
  · exact ⟨rfl, rfl, rfl, rfl, rfl, rfl, ⟨[("value", 3)], rfl, rfl⟩⟩
  · decide
  · decide
def twoFrameBody : NumericExpr := .assign "value" (.add (.name "value") (.name "delta")) (.name "value")
def twoFrameLeft : ExecutionState Unit := ⟨[], [], [], none, [], none, [("left-first", 1), ("left-second", 10)]⟩
def twoFrameRight : ExecutionState Unit := ⟨[], [], [], none, [], none, [("right-first", 1), ("right-second", 10)]⟩
def twoFrameLeftFinal : ExecutionState Unit :=
  ⟨[], [], [], none, [some 3, some 13, some 7, some 14], none, [("left-first", 7), ("left-second", 14)]⟩
def twoFrameRightFinal : ExecutionState Unit :=
  ⟨[], [], [], none, [some 3, some 13, some 7, some 14], none, [("right-first", 7), ("right-second", 14)]⟩
theorem twoFrameExample : twoCapturedStatesRelated [("value", "left-first")] [("value", "right-first")]
    [("value", "left-second")] [("value", "right-second")] twoFrameLeftFinal twoFrameRightFinal := by
  refine two_captured_call_sequence_related [("value", "left-first")] [("value", "right-first")]
    [("value", "left-second")] [("value", "right-second")] ?_ ?_
    [.invokeCaptured [("value", "left-first")] ["delta"] [2] twoFrameBody,
     .invokeCaptured [("value", "left-second")] ["delta"] [3] twoFrameBody,
     .invokeCaptured [("value", "left-first")] ["delta"] [4] twoFrameBody,
     .invokeCaptured [("value", "left-second")] ["delta"] [1] twoFrameBody]
    [.invokeCaptured [("value", "right-first")] ["delta"] [2] twoFrameBody,
     .invokeCaptured [("value", "right-second")] ["delta"] [3] twoFrameBody,
     .invokeCaptured [("value", "right-first")] ["delta"] [4] twoFrameBody,
     .invokeCaptured [("value", "right-second")] ["delta"] [1] twoFrameBody]
    ?_ twoFrameLeft twoFrameRight twoFrameLeftFinal twoFrameRightFinal ?_ ?_ ?_
  · intro name key member otherName otherKey otherMember
    cases member with
    | head =>
      cases otherMember with
      | head => decide
      | tail _ impossible => cases impossible
    | tail _ impossible => cases impossible
  · intro name key member otherName otherKey otherMember
    cases member with
    | head =>
      cases otherMember with
      | head => decide
      | tail _ impossible => cases impossible
    | tail _ impossible => cases impossible
  · exact .cons (.first ["delta"] [2] twoFrameBody)
      (.cons (.second ["delta"] [3] twoFrameBody)
        (.cons (.first ["delta"] [4] twoFrameBody) (.cons (.second ["delta"] [1] twoFrameBody) .nil)))
  · exact ⟨⟨rfl, rfl, rfl, rfl, rfl, rfl, ⟨[("value", 1)], rfl, rfl⟩⟩,
      ⟨rfl, rfl, rfl, rfl, rfl, rfl, ⟨[("value", 10)], rfl, rfl⟩⟩⟩
  · decide
  · decide
#print axioms twoFrameExample
`,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_future_calls' does not depend on any axioms/);
  assert.match(replay.stdout, /'captured_frame_evaluation_preserved' does not depend on any axioms/);
  assert.match(replay.stdout, /'binding_captured_observations' does not depend on any axioms/);
  assert.match(replay.stdout, /'captured_frame_writeback_preserved' does not depend on any axioms/);
  assert.match(replay.stdout, /'captured_call_frame_preserved' does not depend on any axioms/);
  assert.match(replay.stdout, /'twoFrameExample' does not depend on any axioms/);
  assert.match(replay.stdout, /'captured_completion_preserved' does not depend on any axioms/);
  assert.match(replay.stdout, /'captured_machine_step_preserved' does not depend on any axioms/);
  assert.match(replay.stdout, /'runExecution_relation_preserved' does not depend on any axioms/);
  assert.match(replay.stdout, /'executeCaptured_success' does not depend on any axioms/);
  assert.match(replay.stdout, /'captured_paired_step_preserved' does not depend on any axioms/);
  const emitted = join(out, "alias.mjs");
  await writeFile(emitted, (await readFile(emitted, "utf8")).replaceAll("first", "second"));
  assert.notEqual(cli("validate", source, spec, out).result, "proved under assumptions");
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
  const recursiveSource = join(root, "shadowed-recursion.mjs");
  const recursiveSpec = join(root, "shadowed-recursion.json");
  await writeFile(
    recursiveSource,
    `function increment(step) { return step; }
function make(start) { function increment(step) { return increment(step); } return increment; }
const first = make(1);
export { first };
`
  );
  await writeFile(
    recursiveSpec,
    JSON.stringify({
      kind: "bindings",
      entry: "entry.mjs",
      modules: { "entry.mjs": { imports: [], statements: [0, 1, 2] } },
    })
  );
  assert.equal(cli("generate", recursiveSource, recursiveSpec, join(root, "shadowed-recursion-out")).result, "unknown");
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
  assert.notEqual(cli("validate", source, spec, out).result, "proved under assumptions");
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
function select(x, y) { if (x < y && y === 4 && x !== 3 && x >= 2) return 9; return 1; }
function short(x) { if (x === 0 || maybe(x) >= maybe(0)) return 3; return 4; }
function invert(x) { if (!x) return 8; return 2; }
function conjunction(x) { if (x && maybe(1)) return 1; return 2; }
function difference(x, y) { let value = x + 5; value -= y; value--; return value - 1; }
function greater(x, y) { if (x > y) return 7; return 6; }
function atMost(x, y) { if (x <= y) return 5; return 4; }
function other(value) { if (value) throw 8; return value; }
function order(x) { if (maybe(x) > other(x)) return 1; return 0; }
export { select, short, invert, conjunction, difference, greater, atMost, order };
`
  );
  await writeFile(
    spec,
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
  assert.equal(cli("generate", source, spec, out).result, "generated");
  const proof = cli("validate", source, spec, out);
  assert.equal(proof.result, "proved under assumptions", JSON.stringify(proof));
  const calls = [
    { name: "select", args: [2, 4] },
    { name: "select", args: [5, 4] },
    { name: "select", args: [2, 3] },
    { name: "select", args: [3, 4] },
    { name: "select", args: [1, 4] },
    { name: "difference", args: [4, 3] },
    { name: "invert", args: [0] },
    { name: "invert", args: [2] },
    { name: "greater", args: [3, 2] },
    { name: "greater", args: [2, 2] },
    { name: "atMost", args: [2, 2] },
    { name: "atMost", args: [3, 2] },
    { name: "short", args: [0] },
    { name: "short", args: [1] },
  ];
  const trace = execute(source, calls);
  assert.deepEqual(trace.results, [
    { value: 9 },
    { value: 1 },
    { value: 1 },
    { value: 1 },
    { value: 1 },
    { value: 4 },
    { value: 8 },
    { value: 2 },
    { value: 7 },
    { value: 6 },
    { value: 5 },
    { value: 4 },
    { value: 3 },
  ]);
  assert.deepEqual(trace.error, { name: null, phase: "call", index: 13, thrown: { kind: "number", value: 7 } });
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
  const orderCalls = [{ name: "order", args: [1] }];
  const orderTrace = execute(source, orderCalls);
  assert.deepEqual(orderTrace.error, { name: null, phase: "call", index: 0, thrown: { kind: "number", value: 7 } });
  assert.deepEqual(execute(join(out, "entry.mjs"), orderCalls), orderTrace);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input:
      models +
      proof.certificate +
      "\nexample : evaluateExpression [] [] (.subtract (.literal 1) (.literal 2)) = none := by decide\n",
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_future_calls' does not depend on any axioms/);
  const logic = join(out, "logic.mjs");
  const originalLogic = await readFile(logic, "utf8");
  const changedSubtraction = originalLogic.replace("return value - 1", "return value - 2");
  assert.notEqual(changedSubtraction, originalLogic);
  await writeFile(logic, changedSubtraction);
  assert.notEqual(cli("validate", source, spec, out).result, "proved under assumptions");
  const subtractionCalls = [{ name: "difference", args: [4, 3] }];
  assert.notDeepEqual(execute(source, subtractionCalls), execute(join(out, "entry.mjs"), subtractionCalls));
  await writeFile(logic, originalLogic);
  await writeFile(logic, (await readFile(logic, "utf8")).replace("x < y", "x === y"));
  assert.notEqual(cli("validate", source, spec, out).result, "proved under assumptions");
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
function make(start) { let value = start * seed - 1; return (step) => { try { if (step < value) return value; return step; } finally { value += 1; } }; }
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
  assert.deepEqual(execute(source, witness.calls).results, [{ value: 3 }]);
  assert.deepEqual(execute(join(out, "entry.mjs"), witness.calls).results, [{ value: 1 }]);
  const replay = spawnSync(process.env.JS_MODULE_PROOF_LEAN, ["-j", "1", "--trust=0", "--stdin"], {
    encoding: "utf8",
    input: models + witness.certificate,
  });
  assert.equal(replay.status, 0, replay.stdout + replay.stderr);
  assert.match(replay.stdout, /'binding_witness_difference' does not depend on any axioms/);
}
