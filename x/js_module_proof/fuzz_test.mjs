import assert from "node:assert/strict";
import { access, writeFile } from "node:fs/promises";
import { isAbsolute, join, resolve } from "node:path";
import { createRequire } from "node:module";

if (process.env.ACORN_RLOCATION)
  process.env.JS_MODULE_PROOF_ACORN = resolve(process.env.TEST_SRCDIR, process.env.ACORN_RLOCATION);
process.env.JS_MODULE_PROOF_LEAN = resolve(process.env.TEST_SRCDIR, process.env.LEAN_RLOCATION);
if (process.env.TEST_SHARD_STATUS_FILE) await writeFile(process.env.TEST_SHARD_STATUS_FILE, "");
const { execute, independentSplit, makeCase, makeComposedCase, minimizeCase, runFuzz } = await import("./fuzz.mjs");
const { assessCorpus } = await import("./real_corpus.mjs");
const shard = Number(process.env.TEST_SHARD_INDEX ?? 0);
const shards = Number(process.env.TEST_TOTAL_SHARDS ?? 1);
if (shards === 4 && shard === 1) {
  const relay = await runFuzz({
    seed: 51981,
    start: 3,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "composed",
    validateEvery: 1,
  });
  assert.equal(relay.reexportRelayCases, 1);
  assert.equal(relay.reexportRelayGeneratedCases, 1);
  assert.equal(relay.reexportRelayComparedCases, 1);
  assert.equal(relay.reexportRelayProvedCases, 1);
  assert.equal(relay.discardedLiteralCases, 1);
  assert.equal(relay.discardedLiteralComparedCases, 1);
  assert.equal(relay.discardedLiteralProvedCases, 1);
  assert.equal(relay.textObservationCases, 1);
  assert.equal(relay.textObservationComparedCases, 1);
  assert.equal(relay.textObservationProvedCases, 1);
  assert.equal(relay.primitiveObservationCases, 1);
  assert.equal(relay.primitiveObservationComparedCases, 1);
  assert.equal(relay.primitiveObservationProvedCases, 1);
  assert.equal(relay.undefinedObservationCases, 1);
  assert.equal(relay.undefinedObservationComparedCases, 1);
  assert.equal(relay.undefinedObservationProvedCases, 1);
}
if (shards === 4 && shard === 3) {
  const mutated = await runFuzz({
    seed: 51981,
    start: 16,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "composed",
    validateEvery: 1,
  });
  assert.equal(mutated.mutationKinds.composedEffect, 1);
  assert.equal(mutated.mutatedDifferent, 1);
  assert.equal(mutated.mutatedVerdicts.unsafe, 1);
}
assert.deepEqual(makeCase(123, 42), makeCase(123, 42));
assert.notDeepEqual(makeCase(123, 42), makeCase(124, 42));
const scopedCandidate = makeCase(123, 11);
assert.ok(Object.values(scopedCandidate.spec.modules).filter((module) => module.statements.length).length > 1);
await assert.rejects(
  runFuzz({ seed: 1, cases: 1, outputDir: process.env.TEST_TMPDIR, shard: 2, shards: 2 }),
  /shard must/
);
const shrinkInput = makeCase(123, 0);
const keptNode = JSON.stringify(shrinkInput.ast.body[0]);
const keptCall = shrinkInput.calls[0];
const shrunk = await minimizeCase(
  shrinkInput,
  async (candidate) =>
    candidate.ast.body.some((node) => JSON.stringify(node) === keptNode) && candidate.calls.includes(keptCall)
);
assert.equal(shrunk.ast.body.filter((node) => node.type !== "ExportNamedDeclaration").length, 1);
assert.deepEqual(shrunk.calls, [keptCall]);
assert.equal(Object.values(shrunk.spec.modules).flatMap((module) => module.statements).length, 1);
assert.deepEqual(
  Object.values(shrunk.spec.modules).flatMap((module) => module.statements),
  [0]
);
assert.ok(Object.keys(shrunk.spec.modules).length <= 2);
const relayShrinkInput = makeComposedCase(51981, 3);
const doubleRelay = makeComposedCase(51981, 11);
assert.deepEqual(doubleRelay.spec.modules["relay2.mjs"].reexports, {
  "relay.mjs": doubleRelay.spec.modules["relay.mjs"].reexports["state.mjs"],
});
relayShrinkInput.spec.modules["relay.mjs"].reexports["state.mjs"].push("unused");
const relayShrunk = await minimizeCase(relayShrinkInput, async (candidate) => {
  const relay = candidate.spec.modules["relay.mjs"];
  return Boolean(relay?.imports.includes("state.mjs") && relay.reexports?.["state.mjs"]?.includes("global"));
});
assert.deepEqual(relayShrunk.spec.modules["relay.mjs"].reexports["state.mjs"], ["global"]);
const generatedObjects = Array.from({ length: 32 }, (_, i) => makeCase(123, 20 + 64 * i));
assert.equal(new Set(generatedObjects.map((item) => Object.keys(item.spec.modules).length)).size, 2);
assert.ok(
  new Set(generatedObjects.map((item) => item.ast.body[1].body.body[2].declarations[0].init.elements.length)).size > 1
);
assert.ok(
  new Set(
    generatedObjects.map((item) => item.ast.body[1].body.body.filter((node) => node.type === "IfStatement").length)
  ).size > 1
);
assert.equal(new Set(generatedObjects.map((item) => item.ast.body[1].body.body.at(-1).argument.left.computed)).size, 2);
const captureTargets = new Set(Array.from({ length: 32 }, (_, i) => makeCase(123, 28 + 128 * i).captureTarget));
assert.deepEqual(captureTargets, new Set(["value", "start"]));
const captureCounts = new Set(
  Array.from(
    { length: 64 },
    (_, i) =>
      makeCase(123, 28 + 128 * i).ast.body.filter(
        (node) => node.type === "VariableDeclaration" && node.declarations[0].id.name.startsWith("closure")
      ).length
  )
);
assert.deepEqual(captureCounts, new Set([1, 2, 3, 4, 5]));
const expressionInput = makeCase(123, 28);
const expressionShrunk = await minimizeCase(
  expressionInput,
  async (candidate) =>
    candidate.source.includes("function make(") &&
    candidate.source.includes("return (step) =>") &&
    candidate.ast.body.some(
      (node) =>
        node.type === "FunctionDeclaration" &&
        node.id.name === "make" &&
        node.body.body.some((item) => item.type === "VariableDeclaration" && item.declarations[0].id.name === "value")
    )
);
assert.match(expressionShrunk.source, /let value = (start|seed);/);
const throwing = join(process.env.TEST_TMPDIR, `throwing-${shard}.mjs`);
await writeFile(throwing, "export function first() { return 42; } export function fail() { throw new Error(); }");
assert.deepEqual(execute(throwing, ["first", "fail"]), {
  events: [],
  exports: ["fail", "first"],
  results: [{ value: 42 }],
  error: { name: "Error", phase: "call", index: 1, thrown: { kind: "opaque", name: "Error" } },
});
const primitiveThrow = join(process.env.TEST_TMPDIR, `primitive-throw-${shard}.mjs`);
await writeFile(primitiveThrow, "export function first() { return 42; } export function fail() { throw 'changed'; }");
assert.deepEqual(execute(primitiveThrow, ["first", "fail"]), {
  events: [],
  exports: ["fail", "first"],
  results: [{ value: 42 }],
  error: { name: null, phase: "call", index: 1, thrown: { kind: "string", value: "changed" } },
});
const nullThrow = join(process.env.TEST_TMPDIR, `null-throw-${shard}.mjs`);
await writeFile(nullThrow, "export function fail() { throw null; }");
assert.deepEqual(execute(nullThrow, ["fail"]).error, {
  name: null,
  phase: "call",
  index: 0,
  thrown: { kind: "null" },
});
const opaqueThrow = join(process.env.TEST_TMPDIR, `opaque-throw-${shard}.mjs`);
await writeFile(opaqueThrow, "export function fail() { throw { value: 1 }; }");
assert.equal(execute(opaqueThrow, ["fail"]).error.thrown.kind, "opaque");
const asyncCalls = join(process.env.TEST_TMPDIR, `async-${shard}.mjs`);
await writeFile(
  asyncCalls,
  "export async function first() { await Promise.resolve(); globalThis.record(1); return 4; } export async function fail() { await Promise.resolve(); globalThis.record(2); throw 'async'; }"
);
assert.deepEqual(execute(asyncCalls, ["first", "fail"]), {
  events: [1, 2],
  exports: ["fail", "first"],
  results: [{ value: 4 }],
  error: { name: null, phase: "call", index: 1, thrown: { kind: "string", value: "async" } },
});
const pendingCall = join(process.env.TEST_TMPDIR, `async-pending-${shard}.mjs`);
await writeFile(pendingCall, "export async function wait() { await new Promise(() => {}); }");
assert.deepEqual(execute(pendingCall, ["wait"]), {
  events: [],
  exports: ["wait"],
  results: [],
  error: null,
  inconclusive: "no completion",
});
const primitives = join(process.env.TEST_TMPDIR, `primitives-${shard}.mjs`);
await writeFile(
  primitives,
  "export function nan() { globalThis.record(Infinity); return NaN; } export function negzero() { return -0; } export function big() { return 9n; } export function object() { return {}; }"
);
const primitiveTrace = execute(primitives, ["nan", "negzero", "big"]);
assert.deepEqual(primitiveTrace.events, [{ number: "Infinity" }]);
assert.deepEqual(primitiveTrace.results, [
  { value: { number: "NaN" } },
  { value: { number: "-0" } },
  { value: { bigint: "9" } },
]);
assert.equal(execute(primitives, ["object"]).inconclusive, "opaque observation");
const hostileThrow = join(process.env.TEST_TMPDIR, `hostile-throw-${shard}.mjs`);
await writeFile(hostileThrow, "export function fail() { throw { get name() { throw 9; } }; }");
assert.deepEqual(execute(hostileThrow, ["fail"]).error, {
  name: null,
  phase: "call",
  index: 0,
  thrown: { kind: "opaque" },
});
const noisy = join(process.env.TEST_TMPDIR, `noisy-${shard}.mjs`);
await writeFile(noisy, "export function emit() { for (let i = 0; i < 200000; i++) globalThis.record('long event'); }");
const noisyTrace = execute(noisy, ["emit"]);
assert.equal(noisyTrace.inconclusive, "output limit");
assert.ok(noisyTrace.events.length > 0);
const timeout = join(process.env.TEST_TMPDIR, `timeout-${shard}.mjs`);
await writeFile(timeout, "globalThis.record(3); while (true) {}");
assert.deepEqual(execute(timeout, [], { timeoutMs: 2000 }), {
  events: [3],
  exports: [],
  results: [],
  error: null,
  inconclusive: "timeout",
});
const result = await runFuzz({ seed: 123, cases: 16, outputDir: process.env.TEST_TMPDIR, shard, shards });
console.log(JSON.stringify(result));
assert.equal(result.cases, 16 / shards);
assert.equal(result.astNodeCases.Program, result.cases);
assert.equal(
  Object.values(result.moduleCountCases).reduce((a, b) => a + b, 0),
  result.cases
);
assert.equal(
  Object.values(result.importCountCases).reduce((a, b) => a + b, 0),
  result.cases
);
if (shards === 4 && shard === 0) {
  const runfiles = resolve(process.env.TEST_SRCDIR, process.env.TEST_WORKSPACE);
  const corpusOutput = join(process.env.TEST_UNDECLARED_OUTPUTS_DIR ?? process.env.TEST_TMPDIR, "real-corpus");
  const corpus = await assessCorpus(
    [
      join(runfiles, "x/js_module_proof/example/bindings/original.mjs"),
      join(runfiles, "devinfra/pr_visuals/gallery_sort.js"),
    ],
    corpusOutput
  );
  await writeFile(join(corpusOutput, "summary.json"), JSON.stringify(corpus, null, 2) + "\n");
  assert.equal(corpus.programs, 2);
  assert.equal(corpus.assignments, 6);
  assert.equal(corpus.proved + corpus.unsafe + corpus.unknown, 6);
  assert.equal(corpus.generationRefused + corpus.validationUnknown, corpus.unknown);
  assert.equal(
    Object.values(corpus.unknownReasons).reduce((sum, count) => sum + count, 0),
    corpus.unknown
  );
  assert.ok(corpus.generationRefused > 0);
  assert.ok(
    Object.keys(corpus.unknownReasons).some((reason) => reason.includes("expected an identifier, got MemberExpression"))
  );
  assert.equal(
    corpus.entries.find(
      (entry) => entry.path.endsWith("example/bindings/original.mjs") && entry.assignment === "baseline"
    ).result,
    "proved under assumptions"
  );
  assert.equal(
    corpus.entries.find(
      (entry) => entry.path.endsWith("example/bindings/original.mjs") && entry.assignment === "contiguous"
    ).result,
    "proved under assumptions"
  );
  const generatedExpressions = Array.from({ length: 128 }, (_, i) => makeCase(123, 12 + 32 * i));
  assert.deepEqual(new Set(generatedExpressions.map((item) => item.expressionDepth)), new Set([0, 1, 2]));
  assert.deepEqual(
    new Set(generatedExpressions.map((item) => item.spec.modules["state.mjs"].statements.includes(1))),
    new Set([false, true])
  );
  for (const helperInState of [false, true]) {
    const index = Array.from({ length: 128 }, (_, i) => 12 + 32 * i).find(
      (candidate) => makeCase(123, candidate).spec.modules["state.mjs"].statements.includes(1) === helperInState
    );
    const checked = await runFuzz({ seed: 123, start: index, cases: 1, outputDir: process.env.TEST_TMPDIR });
    assert.equal(checked.parameterCompared, 1);
    assert.equal(checked.parameterProved, 1);
  }
  for (const [index, item] of generatedExpressions.slice(0, 16).entries()) {
    const path = join(process.env.TEST_TMPDIR, `parameter-source-${index}.mjs`);
    await writeFile(path, item.source);
    const observed = execute(path, item.calls);
    assert.equal(observed.error?.index, 1);
    assert.deepEqual(observed.error?.thrown, item.expectedThrown);
  }
  assert.ok(result.proved > 0);
  assert.ok(result.constantCases > 0);
  assert.equal(result.parameterCases, 1);
  assert.equal(result.parameterCompared, 1);
  assert.equal(result.parameterProved, 1);
  const changedExport = await runFuzz({
    seed: 123,
    start: 26,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    validateEvery: 1,
  });
  assert.equal(changedExport.mutationKinds.export, 1);
  assert.equal(changedExport.mutatedVerdicts.unsafe, 1);
  const objectCase = await runFuzz({ seed: 123, start: 20, cases: 1, outputDir: process.env.TEST_TMPDIR });
  assert.equal(objectCase.objectCases, 1);
  assert.equal(objectCase.objectSourceExecuted, 1);
  assert.equal(objectCase.objectCompared, 1);
  assert.equal(objectCase.proved, 1);
  assert.equal(objectCase.objectProved, 1);
  const multiply = await runFuzz({ seed: 123, start: 108, cases: 1, outputDir: process.env.TEST_TMPDIR });
  assert.equal(multiply.parameterCompared, 1);
  assert.equal(multiply.parameterProved, 1);
  const greaterEqual = await runFuzz({ seed: 123, start: 172, cases: 1, outputDir: process.env.TEST_TMPDIR });
  assert.equal(greaterEqual.parameterCompared, 1);
  assert.equal(greaterEqual.parameterProved, 1);
  for (const index of [236, 300]) {
    const comparison = await runFuzz({ seed: 123, start: index, cases: 1, outputDir: process.env.TEST_TMPDIR });
    assert.equal(comparison.parameterCompared, 1);
    assert.equal(comparison.parameterProved, 1);
  }
  const captured = await runFuzz({ seed: 123, start: 28, cases: 1, outputDir: process.env.TEST_TMPDIR });
  assert.equal(captured.capturedCompared, 1);
  assert.equal(captured.capturedProved, 1);
  assert.equal(captured.renamedExportCases, 1);
  assert.equal(captured.multiplePublicNameCases, 1);
  const namedCaptured = await runFuzz({ seed: 123, start: 156, cases: 1, outputDir: process.env.TEST_TMPDIR });
  assert.equal(namedCaptured.capturedCompared, 1);
  assert.equal(namedCaptured.capturedProved, 1);
  for (const index of [284, 412]) {
    const expressionCaptured = await runFuzz({ seed: 123, start: index, cases: 1, outputDir: process.env.TEST_TMPDIR });
    assert.equal(expressionCaptured.capturedCompared, 1);
    assert.equal(expressionCaptured.capturedProved, 1);
  }
  const parameterIndex = Array.from({ length: 32 }, (_, i) => 28 + 128 * i).find(
    (index) => makeCase(123, index).captureTarget === "start"
  );
  const capturedParameter = await runFuzz({
    seed: 123,
    start: parameterIndex,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
  });
  assert.equal(capturedParameter.capturedProved, 1);
  const capturedUnsafe = await runFuzz({ seed: 123, start: 92, cases: 1, outputDir: process.env.TEST_TMPDIR });
  assert.equal(capturedUnsafe.capturedCompared, 1);
  assert.equal(capturedUnsafe.capturedUnsafe, 1);
}
if (shards === 4 && shard === 2) {
  const original = makeCase(123, 2);
  const independent = independentSplit(original, 123, 2);
  assert.deepEqual(independent, independentSplit(original, 123, 2));
  assert.equal(independent.source, original.source);
  assert.deepEqual(
    Object.values(independent.spec.modules)
      .flatMap((module) => module.statements)
      .sort((a, b) => a - b),
    original.ast.body.filter((node) => node.type !== "ExportNamedDeclaration").map((_, i) => i)
  );
  const campaign = await runFuzz({
    seed: 123,
    cases: 32,
    outputDir: process.env.TEST_TMPDIR,
    splitMode: "independent",
    validateEvery: 16,
  });
  assert.equal(campaign.generated + campaign.independentRefused, campaign.cases);
  assert.ok(campaign.refused > 0);
  assert.equal(
    Object.values(campaign.refusalReasons).reduce((sum, count) => sum + count, 0),
    campaign.refused
  );
  assert.equal(campaign.splitMode, "independent");
  assert.ok(campaign.importAliasRequestedCases > 0);
  assert.ok(campaign.importAliasGeneratedCases > 0);
  assert.deepEqual(campaign.independentRequested, { acyclic: 24, cyclic: 4, invalidAssignment: 4 });
  assert.equal(campaign.independentOrdered, 4);
  assert.ok(campaign.independentOrderedRefused <= campaign.independentOrdered);
  assert.deepEqual(makeComposedCase(1, 2), makeComposedCase(1, 2));
  const composed = await runFuzz({
    seed: 7,
    cases: 16,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "composed",
    validateEvery: 4,
  });
  assert.equal(composed.compared, 16);
  assert.equal(composed.equal, 16);
  assert.equal(
    Object.values(composed.refusalReasons).reduce((sum, count) => sum + count, 0),
    composed.refused
  );
  assert.ok(composed.proved > 0);
  assert.ok(composed.renamedExportCases > 0);
  assert.ok(composed.multiplePublicNameCases > 0);
  assert.equal(composed.importAliasRequestedCases, 4);
  assert.ok(composed.importAliasGeneratedCases > 0);
  assert.equal(composed.importAliasComparedCases, composed.importAliasGeneratedCases);
  assert.ok(composed.astNodeCases.IfStatement > 0);
  const checkedAlias = await runFuzz({
    seed: 51981,
    start: 0,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "composed",
    validateEvery: 1,
  });
  assert.equal(checkedAlias.importAliasRequestedCases, 1);
  assert.equal(checkedAlias.importAliasGeneratedCases, 1);
  assert.equal(checkedAlias.importAliasComparedCases, 1);
  assert.equal(checkedAlias.importAliasProvedCases, 1);
  assert.equal(checkedAlias.varCases, 1);
  assert.equal(checkedAlias.varGeneratedCases, 1);
  assert.equal(checkedAlias.varComparedCases, 1);
  assert.equal(checkedAlias.varProvedCases, 1);
  assert.equal(checkedAlias.localVarCases, 1);
  assert.equal(checkedAlias.localVarComparedCases, 1);
  assert.equal(checkedAlias.localVarProvedCases, 1);
  assert.equal(checkedAlias.earlyVarCases, 1);
  assert.equal(checkedAlias.earlyVarComparedCases, 1);
  assert.equal(checkedAlias.earlyVarProvedCases, 1);
  assert.equal(checkedAlias.earlyVarDifferentCases, 0);
  assert.equal(checkedAlias.earlyVarUnsafeCases, 0);
  const reorderedVar = await runFuzz({
    seed: 51981,
    start: 14,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "composed",
    splitMode: "independent",
    validateEvery: 1,
  });
  assert.equal(reorderedVar.plannedEarlyVarReorderCases, 1);
  assert.equal(reorderedVar.earlyVarComparedCases, 1);
  assert.equal(reorderedVar.earlyVarDifferentCases, 1);
  assert.equal(reorderedVar.earlyVarUnsafeCases, 1);
  const asyncCampaign = await runFuzz({ seed: 37, cases: 8, outputDir: process.env.TEST_TMPDIR, grammarMode: "async" });
  assert.equal(asyncCampaign.asyncSourceExecuted, 8);
  assert.equal(asyncCampaign.inconclusive, 0);
}
if (shards === 4 && shard === 1) assert.equal(result.unsafe, result.cases);
if (shards === 4 && shard === 3) {
  assert.equal(result.closureCases + result.modeledClosureCases, result.cases);
  assert.equal(result.modeledClosureCases, 1);
  assert.equal(result.modeledClosureCompared, 1);
  assert.equal(result.modeledClosureProved, 1);
  assert.equal(result.modeledClosureThrows, 1);
  assert.equal(result.modeledClosureBranches, 1);
  assert.equal(result.closureSourceExecuted, result.closureCases);
  assert.equal(result.closureRefused + result.closureGenerated, result.closureCases);
  assert.ok(result.closureCompared > 0);
  const unsafeClosure = await runFuzz({ seed: 123, start: 19, cases: 1, outputDir: process.env.TEST_TMPDIR });
  assert.equal(unsafeClosure.modeledClosureCompared, 1);
  assert.equal(unsafeClosure.modeledClosureUnsafe, 1);
  assert.equal(unsafeClosure.modeledClosureBranches, 1);
}
if (shards === 4 && shard === 2) {
  assert.ok(result.mutated > 0);
  assert.ok(result.different > 0);
}
assert.equal(result.generated, result.compared + result.inconclusive);
assert.equal(result.refused, result.unsupportedCaseRefused + result.invalidAssignmentCaseRefused);
assert.equal(
  result.validated,
  Object.values(result.mutatedVerdicts).reduce((a, b) => a + b, 0) +
    Object.values(result.unmodifiedVerdicts).reduce((a, b) => a + b, 0)
);

// Exercise many additional programs against Node without paying for a Lean
// invocation on every case. Validation is sampled independently by index.
const bulkCases = Number(process.env.JS_MODULE_PROOF_FUZZ_CASES ?? "128");
const bulkValidateEvery = Number(process.env.JS_MODULE_PROOF_FUZZ_VALIDATE_EVERY ?? "1000000000");
assert.ok(Number.isSafeInteger(bulkCases) && bulkCases >= 128 && bulkCases % 64 === 0);
assert.ok(Number.isSafeInteger(bulkValidateEvery) && bulkValidateEvery >= 1);
const bulkOutput = join(process.env.TEST_UNDECLARED_OUTPUTS_DIR ?? process.env.TEST_TMPDIR, "bulk");
const bulk = await runFuzz({
  seed: 456,
  cases: bulkCases,
  outputDir: bulkOutput,
  shard,
  shards,
  validateEvery: bulkValidateEvery,
});
assert.equal(bulk.cases, bulkCases / shards);
assert.equal(bulk.astNodeCases.Program, bulk.cases);
assert.ok(bulk.astNodeCases.FunctionDeclaration > 0);
assert.equal(bulk.generated, bulk.compared + bulk.inconclusive);
assert.equal(bulk.refused, bulk.unsupportedCaseRefused + bulk.invalidAssignmentCaseRefused);
if (shards === 4 && shard === 0) {
  assert.equal(bulk.objectCases, bulkCases / 64);
  assert.equal(bulk.objectSourceExecuted, bulk.objectCases);
  assert.equal(bulk.objectRefused + bulk.objectCompared, bulk.objectCases);
}
if (shards === 4 && shard === 1) assert.equal(bulk.different, bulk.cases);
if (shards === 4 && shard === 2)
  for (const [kind, count] of Object.entries(bulk.mutationKinds)) if (kind !== "composedEffect") assert.ok(count > 0);
if (shards === 4 && shard === 3) {
  assert.equal(bulk.closureSourceExecuted, bulk.closureCases);
  assert.ok(bulk.finallyCases > 0);
  assert.equal(bulk.finallySourceExecuted, bulk.finallyCases);
  assert.equal(bulk.modeledClosureCases, bulk.cases / 4);
  assert.equal(bulk.modeledClosureCompared, bulk.modeledClosureCases);
  assert.ok(bulk.modeledClosureCrossModule > 0);
  assert.equal(bulk.closureRefused + bulk.closureGenerated, bulk.closureCases);
  assert.ok(bulk.closureCompared > 0);
  assert.equal(bulk.controlFlowCases, bulk.cases / 2);
  assert.ok(bulk.branchThen > 0);
  assert.ok(bulk.branchElse > 0);
}
for (const difference of bulk.unexplainedDifferences) {
  assert.ok(!isAbsolute(difference.artifactPath));
  await access(join(bulkOutput, difference.artifactPath, "node.json"));
}
await writeFile(join(bulkOutput, "summary.json"), JSON.stringify(bulk, null, 2) + "\n");
console.log(JSON.stringify(bulk));

// Run structural sampling after the Lean-backed cases to limit peak memory.
const parse = createRequire(import.meta.url)(process.env.JS_MODULE_PROOF_ACORN ?? "acorn").parse;
const composedPrograms = Array.from({ length: 192 }, (_, i) => makeComposedCase(917, i));
assert.deepEqual(
  new Set(
    composedPrograms
      .filter((item) => item.hasDiscardedLiteral && item.ast.body[0].expression.type === "Literal")
      .map((item) => item.ast.body[0].expression.value)
  ),
  new Set(["use strict", true, null, 42])
);
assert.deepEqual(
  new Set(
    composedPrograms
      .filter((item) => item.hasDiscardedLiteral && item.ast.body[0].expression.type === "UnaryExpression")
      .map((item) => item.ast.body[0].expression.operator)
  ),
  new Set(["void", "!", "typeof"])
);
assert.ok(new Set(composedPrograms.map((item) => item.source)).size > 120);
assert.ok(composedPrograms.some((item) => item.source.includes(" as public_calculate")));
assert.ok(composedPrograms.some((item) => item.source.includes(" as second_calculate")));
assert.ok(composedPrograms.some((item) => item.spec.modules["logic.mjs"].aliasImports));
assert.ok(composedPrograms.some((item) => item.source.includes("var global")));
assert.ok(composedPrograms.some((item) => item.source.includes("var initialVar")));
assert.ok(composedPrograms.some((item) => item.source.includes("const box =")));
assert.ok(composedPrograms.some((item) => item.source.includes("function nested(")));
assert.ok(composedPrograms.some((item) => item.source.includes("catch {")));
assert.ok(composedPrograms.some((item) => item.source.includes("catch (caught) {")));
assert.ok(composedPrograms.some((item) => item.source.includes(" % ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" %= ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" / ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" /= ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" == ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" != ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" ? ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" && ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" || ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" & ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" &= ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" | ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" ^ ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" |= ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" ^= ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" >>> ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" >>>= ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" << ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" >> ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" <<= ")));
assert.ok(composedPrograms.some((item) => item.source.includes(" >>= ")));
assert.ok(composedPrograms.some((item) => item.source.includes("4294967297")));
assert.ok(
  new Set(composedPrograms.map((item) => item.ast.body.filter((node) => node.type === "FunctionDeclaration").length))
    .size > 1
);
assert.ok(
  composedPrograms.some((item) => {
    const functions = item.ast.body.filter((node) => node.type === "FunctionDeclaration");
    return functions.length > 1 && JSON.stringify(functions[0].body) !== JSON.stringify(functions[1].body);
  })
);
for (const item of composedPrograms) {
  assert.deepEqual(
    parse(item.source, { ecmaVersion: "latest", sourceType: "module" }).body.map((node) => node.type),
    item.ast.body.map((node) => node.type)
  );
}
for (const [index, item] of composedPrograms.slice(0, 16).entries()) {
  const path = join(process.env.TEST_TMPDIR, `composed-${shard}-${index}.mjs`);
  await writeFile(path, item.source);
  const observed = execute(path, item.calls);
  assert.ok(!observed.inconclusive, item.source);
  if (observed.error) assert.ok(["boolean", "null", "string", "number"].includes(observed.error.thrown.kind));
  assert.ok(observed.results.every((result) => Number.isSafeInteger(result.value)));
}
const closures = Array.from({ length: 128 }, (_, i) => makeCase(917, 4 * i + 3)).filter((c) => c.grammar === "closure");
assert.deepEqual(makeCase(917, 15), makeCase(917, 15));
assert.equal(new Set(closures.map((c) => c.depth)).size, 4);
assert.ok(closures.some((c) => c.shadowed));
assert.ok(closures.some((c) => c.writesCaptured));
assert.ok(closures.some((c) => !c.writesCaptured));
assert.equal(closures.filter((c) => c.controlFlow).length, 64);
function countNodes(node, type) {
  if (!node || typeof node !== "object") return 0;
  return (
    (node.type === type ? 1 : 0) +
    Object.values(node).reduce(
      (sum, child) =>
        sum +
        (Array.isArray(child) ? child.reduce((n, item) => n + countNodes(item, type), 0) : countNodes(child, type)),
      0
    )
  );
}
for (const c of closures) {
  assert.equal(countNodes(c.ast, "IfStatement"), c.controlFlow ? 1 : 0);
  assert.equal(countNodes(c.ast, "ForStatement"), c.controlFlow ? 1 : 0);
}
assert.ok(new Set(closures.map((c) => c.source)).size > 80);
for (const c of closures) {
  const parsed = parse(c.source, { ecmaVersion: "latest", sourceType: "module" });
  assert.deepEqual(
    parsed.body.map((n) => n.type),
    c.ast.body.map((n) => n.type)
  );
}
const escaping = closures.find((c) => c.depth === 1 && c.writesCaptured && !c.controlFlow);
assert.ok(escaping);
const closurePath = join(process.env.TEST_TMPDIR, `closure-${shard}.mjs`);
await writeFile(closurePath, escaping.source);
const observedClosure = execute(closurePath, escaping.calls);
assert.equal(observedClosure.error, null);
assert.ok(new Set(observedClosure.results.map((item) => item.value)).size > 1);
assert.deepEqual(execute(closurePath, escaping.calls), observedClosure);
const flow = closures.find((c) => c.controlFlow);
const flowPath = join(process.env.TEST_TMPDIR, `flow-${shard}.mjs`);
await writeFile(flowPath, flow.source);
const observedFlow = execute(flowPath, flow.calls);
assert.equal(observedFlow.error, null);
assert.equal(observedFlow.events.filter((n) => n === 101 || n === 102).length, flow.calls.length);
assert.equal(observedFlow.events.filter((n) => n === 0).length, flow.calls.length);
assert.deepEqual(execute(flowPath, flow.calls), observedFlow);
const examples = Array.from({ length: 100 }, (_, i) => makeCase(123, i));
assert.ok(new Set(examples.map((c) => c.source)).size > 90);
assert.ok(new Set(examples.map((c) => JSON.stringify(c.spec))).size > 90);
