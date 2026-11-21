import assert from "node:assert/strict";
import { access, readFile, writeFile } from "node:fs/promises";
import { isAbsolute, join, resolve } from "node:path";
import { createRequire } from "node:module";

if (process.env.ACORN_RLOCATION)
  process.env.JS_MODULE_PROOF_ACORN = resolve(process.env.TEST_SRCDIR, process.env.ACORN_RLOCATION);
process.env.JS_MODULE_PROOF_LEAN = resolve(process.env.TEST_SRCDIR, process.env.LEAN_RLOCATION);
if (process.env.TEST_SHARD_STATUS_FILE) await writeFile(process.env.TEST_SHARD_STATUS_FILE, "");
const { execute, independentSplit, makeCase, makeComposedCase, makeLocalAliasCase, minimizeCase, runFuzz } =
  await import("./fuzz.mjs");
const { assessCorpus } = await import("./real_corpus.mjs");
const shard = Number(process.env.TEST_SHARD_INDEX ?? 0);
const shards = Number(process.env.TEST_TOTAL_SHARDS ?? 1);
if (shards === 4) {
  const aliases = await runFuzz({
    seed: 773,
    start: shard,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.equal(aliases.localImportAliasRequestedCases, 1);
  assert.equal(aliases.localImportAliasGeneratedCases, 1);
  assert.equal(aliases.localImportAliasComparedCases, 1);
  if (shard >= 1) {
    const mutation = { 1: "localAliasNumericRedirect", 2: "localAliasRedirect", 3: "localAliasInitRedirect" }[shard];
    assert.equal(aliases.mutationKinds[mutation], 1);
    assert.equal(aliases.mutatedDifferent, 1);
    assert.equal(aliases.mutatedVerdicts.unsafe, 1);
    assert.equal(aliases.certifiedWitnesses.length, 1);
    if (shard === 3) {
      const witness = JSON.parse(
        await readFile(
          join(process.env.TEST_TMPDIR, aliases.certifiedWitnesses[0].artifactPath, "node-witness.json"),
          "utf8"
        )
      );
      assert.equal(witness.before.events.length, 2);
      assert.equal(witness.after.events.length, 2);
      assert.deepEqual(witness.before.events[0], witness.after.events[0]);
      assert.notDeepEqual(witness.before.events[1], witness.after.events[1]);
    }
    const unchanged = await runFuzz({
      seed: 773,
      start: shard,
      cases: 1,
      outputDir: process.env.TEST_TMPDIR,
      grammarMode: "alias",
      mutationMode: "disabled",
      validateEvery: 1,
    });
    assert.equal(unchanged.localImportAliasProvedCases, 1);
    assert.equal(unchanged.equal, 1);
  } else {
    assert.equal(aliases.localImportAliasProvedCases, 1);
    assert.equal(aliases.equal, 1);
  }
}
if (shards === 4 && shard === 0) {
  const changedUpdate = await runFuzz({
    seed: 773,
    start: 4,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.equal(changedUpdate.mutationKinds.localAliasUpdateRedirect, 1);
  assert.equal(changedUpdate.mutatedDifferent, 1);
  assert.equal(changedUpdate.mutatedVerdicts.unsafe, 1);
  const witness = JSON.parse(
    await readFile(
      join(process.env.TEST_TMPDIR, changedUpdate.certifiedWitnesses[0].artifactPath, "node-witness.json"),
      "utf8"
    )
  );
  assert.ok(witness.calls.includes("bump"));
  assert.notDeepEqual(witness.before.results, witness.after.results);
  const unchangedUpdate = await runFuzz({
    seed: 773,
    start: 4,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchangedUpdate.localImportAliasProvedCases, 1);
  assert.equal(unchangedUpdate.equal, 1);
}
if (shards === 4 && shard === 0) {
  const changedLocal = await runFuzz({
    seed: 773,
    start: 61,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.match(makeLocalAliasCase(773, 61).source, /const offset = numericLeft/);
  assert.equal(changedLocal.mutatedDifferent, 1);
  assert.equal(changedLocal.mutatedVerdicts.unsafe, 1);
  const unchangedLocal = await runFuzz({
    seed: 773,
    start: 61,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchangedLocal.localImportAliasProvedCases, 1);
  assert.equal(unchangedLocal.equal, 1);
}
if (shards === 4 && shard === 1) {
  const changedLet = await runFuzz({
    seed: 773,
    start: 109,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.match(makeLocalAliasCase(773, 109).source, /let offset = numericLeft/);
  assert.equal(changedLet.mutatedDifferent, 1);
  assert.equal(changedLet.mutatedVerdicts.unsafe, 1);
  const unchangedLet = await runFuzz({
    seed: 773,
    start: 109,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchangedLet.localImportAliasProvedCases, 1);
  assert.equal(unchangedLet.equal, 1);
}
if (shards === 4 && shard === 2) {
  const changedWrite = await runFuzz({
    seed: 773,
    start: 205,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.match(makeLocalAliasCase(773, 205).source, /offset \+= numericRight/);
  assert.equal(changedWrite.mutatedDifferent, 1);
  assert.equal(changedWrite.mutatedVerdicts.unsafe, 1);
  const unchangedWrite = await runFuzz({
    seed: 773,
    start: 205,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchangedWrite.localImportAliasProvedCases, 1);
  assert.equal(unchangedWrite.equal, 1);
}
if (shards === 4 && shard === 0) {
  const changedBranch = await runFuzz({
    seed: 773,
    start: 397,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.match(makeLocalAliasCase(773, 397).source, /if \(\(numericLeft < numericRight\)\)/);
  assert.equal(changedBranch.aliasBranchComparedCases, 1);
  assert.equal(changedBranch.mutatedDifferent, 1);
  assert.equal(changedBranch.mutatedVerdicts.unsafe, 1);
  const unchangedBranch = await runFuzz({
    seed: 773,
    start: 397,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchangedBranch.aliasBranchProvedCases, 1);
  assert.equal(unchangedBranch.localImportAliasProvedCases, 1);
  assert.equal(unchangedBranch.equal, 1);
}
if (shards === 4 && shard === 1) {
  const changedElse = await runFuzz({
    seed: 773,
    start: 493,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.match(makeLocalAliasCase(773, 493).source, /else \{/);
  assert.equal(changedElse.mutatedDifferent, 1);
  assert.equal(changedElse.mutatedVerdicts.unsafe, 1);
  const unchangedElse = await runFuzz({
    seed: 773,
    start: 493,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchangedElse.localImportAliasProvedCases, 1);
  assert.equal(unchangedElse.equal, 1);
}
if (shards === 4 && shard === 2) {
  const changedEarly = await runFuzz({
    seed: 773,
    start: 589,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.match(makeLocalAliasCase(773, 589).source, /return \(offset \+ numericLeft\)/);
  assert.equal(changedEarly.mutatedDifferent, 1);
  assert.equal(changedEarly.mutatedVerdicts.unsafe, 1);
  const unchangedEarly = await runFuzz({
    seed: 773,
    start: 589,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchangedEarly.localImportAliasProvedCases, 1);
  assert.equal(unchangedEarly.equal, 1);
}
if (shards === 4 && shard === 1) {
  const changedLoop = await runFuzz({
    seed: 773,
    start: 781,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.match(makeLocalAliasCase(773, 781).source, /for \(let loopIndex/);
  assert.equal(changedLoop.aliasLoopComparedCases, 1);
  assert.equal(changedLoop.mutatedDifferent, 1);
  assert.equal(changedLoop.mutatedVerdicts.unsafe, 1);
  const unchangedLoop = await runFuzz({
    seed: 773,
    start: 781,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchangedLoop.aliasLoopProvedCases, 1);
  assert.equal(unchangedLoop.localImportAliasProvedCases, 1);
  assert.equal(unchangedLoop.equal, 1);
}
if (shards === 4 && shard === 2) {
  const shadowLoop = await runFuzz({
    seed: 773,
    start: 877,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.match(makeLocalAliasCase(773, 877).source, /for \(let numericLeft/);
  assert.equal(shadowLoop.aliasLoopComparedCases, 1);
  assert.equal(shadowLoop.loopShadowCases, 1);
  assert.equal(shadowLoop.loopShadowComparedCases, 1);
  assert.equal(shadowLoop.equal, 1);
  assert.equal(shadowLoop.loopShadowUnknownCases + shadowLoop.proved, 1);
}
if (shards === 4 && shard === 0) {
  const composedA = makeLocalAliasCase(773, 997);
  const composedB = makeLocalAliasCase(774, 997);
  const body = (input) =>
    input.ast.body.find((node) => node.type === "FunctionDeclaration" && node.id.name === "compute").body;
  assert.notDeepEqual(body(composedA), body(composedB));
  const changedComposed = await runFuzz({
    seed: 773,
    start: 997,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.equal(changedComposed.composedAliasExpressionComparedCases, 1);
  assert.equal(changedComposed.mutatedDifferent, 1);
  assert.equal(changedComposed.mutatedVerdicts.unsafe, 1);
  const unchangedComposed = await runFuzz({
    seed: 773,
    start: 997,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchangedComposed.composedAliasExpressionProvedCases, 1);
  assert.equal(unchangedComposed.equal, 1);
}
if (shards === 4 && shard === 3) {
  const changedTerminal = await runFuzz({
    seed: 773,
    start: 685,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.match(makeLocalAliasCase(773, 685).source, /if \(\(numericOther < numericLeft\)\)/);
  assert.equal(changedTerminal.mutatedDifferent, 1);
  assert.equal(changedTerminal.mutatedVerdicts.unsafe, 1);
  const unchangedTerminal = await runFuzz({
    seed: 773,
    start: 685,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchangedTerminal.localImportAliasProvedCases, 1);
  assert.equal(unchangedTerminal.equal, 1);
}
if (shards === 4 && shard === 3) {
  const changedCompound = await runFuzz({
    seed: 773,
    start: 301,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  const compoundSource = makeLocalAliasCase(773, 301).source;
  assert.match(compoundSource, /offset \*= numericRight/);
  assert.match(compoundSource, /offset\+\+/);
  assert.equal(changedCompound.mutatedDifferent, 1);
  assert.equal(changedCompound.mutatedVerdicts.unsafe, 1);
  const unchangedCompound = await runFuzz({
    seed: 773,
    start: 301,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchangedCompound.localImportAliasProvedCases, 1);
  assert.equal(unchangedCompound.equal, 1);
}
if (shards === 4 && shard === 3) {
  const changedValue = await runFuzz({
    seed: 773,
    start: 49,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.match(makeLocalAliasCase(773, 49).source, /return \(numericLeft \|\| numericRight\)/);
  assert.equal(changedValue.mutatedDifferent, 1);
  assert.equal(changedValue.mutatedVerdicts.unsafe, 1);
  const unchangedValue = await runFuzz({
    seed: 773,
    start: 49,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchangedValue.localImportAliasProvedCases, 1);
  assert.equal(unchangedValue.equal, 1);
}
if (shards === 4 && (shard === 2 || shard === 3)) {
  const start = shard === 2 ? 13 : 25;
  const changedNumeric = await runFuzz({
    seed: 773,
    start,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.equal(changedNumeric.mutationKinds.localAliasNumericRedirect, 1);
  assert.equal(changedNumeric.mutatedDifferent, 1);
  assert.equal(changedNumeric.mutatedVerdicts.unsafe, 1);
  if (start === 13) {
    assert.match(makeLocalAliasCase(773, start).source, /function compute\(numericOther\)/);
    assert.equal(changedNumeric.parameterizedAliasCases, 1);
    assert.equal(changedNumeric.parameterizedAliasComparedCases, 1);
  }
  const unchangedNumeric = await runFuzz({
    seed: 773,
    start,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchangedNumeric.localImportAliasProvedCases, 1);
  assert.equal(unchangedNumeric.equal, 1);
  if (start === 13) assert.equal(unchangedNumeric.parameterizedAliasProvedCases, 1);
}
if (shards === 4 && shard === 1) {
  const changed = await runFuzz({
    seed: 773,
    start: 37,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    validateEvery: 1,
  });
  assert.match(makeLocalAliasCase(773, 37).source, /numericLeft \|\| numericRight/);
  assert.equal(changed.multipleParameterAliasCases, 1);
  assert.equal(changed.multipleParameterAliasComparedCases, 1);
  assert.equal(changed.mutatedDifferent, 1);
  assert.equal(changed.mutatedVerdicts.unsafe, 1);
  assert.equal(changed.certifiedWitnesses[0].calls[0].args.length, 2);
  const unchanged = await runFuzz({
    seed: 773,
    start: 37,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    mutationMode: "disabled",
    validateEvery: 1,
  });
  assert.equal(unchanged.multipleParameterAliasComparedCases, 1);
  assert.equal(unchanged.multipleParameterAliasProvedCases, 1);
  assert.equal(unchanged.equal, 1);
}
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
  const aliasedRelay = await runFuzz({
    seed: 51981,
    start: 11,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "composed",
    validateEvery: 1,
  });
  assert.equal(aliasedRelay.reexportRelayProvedCases, 1);
  assert.equal(aliasedRelay.importAliasGeneratedCases, 1);
  assert.equal(aliasedRelay.importAliasComparedCases, 1);
  assert.equal(aliasedRelay.importAliasProvedCases, 1);

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
  assert.equal(relay.negativeObservationCases, 1);
  assert.equal(relay.negativeObservationComparedCases, 1);
  assert.equal(relay.negativeObservationProvedCases, 1);
  assert.equal(relay.negativeZeroObservationCases, 1);
  assert.equal(relay.negativeZeroObservationComparedCases, 1);
  assert.equal(relay.negativeZeroObservationProvedCases, 1);
  assert.equal(relay.specialNumberObservationCases, 1);
  assert.equal(relay.specialNumberObservationComparedCases, 1);
  assert.equal(relay.specialNumberObservationProvedCases, 1);
  assert.equal(relay.bigIntegerObservationCases, 1);
  assert.equal(relay.bigIntegerObservationComparedCases, 1);
  assert.equal(relay.bigIntegerObservationProvedCases, 1);
  const changedThrow = await runFuzz({
    seed: 51981,
    start: 23,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "composed",
    validateEvery: 1,
    unsafeShrinkAttempts: 1,
  });
  assert.equal(changedThrow.mutationKinds.throwValue, 1);
  assert.equal(changedThrow.mutatedDifferent, 1);
  assert.equal(changedThrow.mutatedVerdicts.unsafe, 1);
  const reducedPath = join(process.env.TEST_TMPDIR, changedThrow.certifiedWitnesses[0].minimizedPath);
  const reducedVerdict = JSON.parse(await readFile(join(reducedPath, "verdict.json"), "utf8"));
  const reducedWitness = JSON.parse(await readFile(join(reducedPath, "node-witness.json"), "utf8"));
  const reduction = JSON.parse(await readFile(join(reducedPath, "reduction.json"), "utf8"));
  assert.equal(reducedVerdict.result, "unsafe");
  assert.notDeepEqual(reducedWitness.before, reducedWitness.after);
  assert.equal(reduction.attempts, 1);
  const changedBigIntegerThrow = await runFuzz({
    seed: 51981,
    start: 87,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "composed",
    validateEvery: 1,
  });
  assert.equal(changedBigIntegerThrow.mutationKinds.throwValue, 1);
  assert.equal(changedBigIntegerThrow.mutatedDifferent, 1);
  assert.equal(changedBigIntegerThrow.mutatedVerdicts.unsafe, 1);
  const changedNullThrow = await runFuzz({
    seed: 51981,
    start: 151,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "composed",
    validateEvery: 1,
  });
  assert.equal(changedNullThrow.mutationKinds.throwValue, 1);
  assert.equal(changedNullThrow.mutatedDifferent, 1);
  assert.equal(changedNullThrow.mutatedVerdicts.unsafe, 1);
  const changedUnicodeThrow = await runFuzz({
    seed: 51981,
    start: 215,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "composed",
    validateEvery: 1,
  });
  assert.equal(changedUnicodeThrow.mutationKinds.throwValue, 1);
  assert.equal(changedUnicodeThrow.mutatedDifferent, 1);
  assert.equal(changedUnicodeThrow.mutatedVerdicts.unsafe, 1);
}
if (shards === 4 && shard === 0) {
  for (const start of [279, 343, 407, 471, 535]) {
    const changedThrow = await runFuzz({
      seed: 51981,
      start,
      cases: 1,
      outputDir: process.env.TEST_TMPDIR,
      grammarMode: "composed",
      validateEvery: 1,
    });
    assert.equal(changedThrow.mutationKinds.throwValue, 1);
    assert.equal(changedThrow.mutatedDifferent, 1);
    assert.equal(changedThrow.mutatedVerdicts.unsafe, 1);
  }
}
if (shards === 4 && shard === 2) {
  const renamedRelay = await runFuzz({
    seed: 51981,
    start: 27,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "composed",
    validateEvery: 1,
  });
  assert.equal(renamedRelay.renamedReexportRequestedCases, 1);
  assert.equal(renamedRelay.renamedReexportGeneratedCases, 1);
  assert.equal(renamedRelay.renamedReexportComparedCases, 1);
  assert.equal(renamedRelay.renamedReexportProvedCases, 1);
}
if (shards === 4 && shard >= 1) {
  const safeCases = { 1: [], 2: [379, 443, 699], 3: [507, 571, 635] };
  const redirectedCases = { 1: [59, 123, 187], 2: [251, 315, 379, 699], 3: [443, 507, 571, 635, 763] };
  for (const start of safeCases[shard]) {
    const unchangedCall = await runFuzz({
      seed: 51981,
      start,
      cases: 1,
      outputDir: process.env.TEST_TMPDIR,
      grammarMode: "composed",
      mutationMode: "disabled",
      validateEvery: 1,
    });
    assert.equal(unchangedCall.equal, 1);
    assert.equal(unchangedCall.mutated, 0);
    assert.equal(unchangedCall.unmodifiedVerdicts.proved, 1);
  }
  for (const start of redirectedCases[shard]) {
    const redirectedRelay = await runFuzz({
      seed: 51981,
      start,
      cases: 1,
      outputDir: process.env.TEST_TMPDIR,
      grammarMode: "composed",
      validateEvery: 1,
    });
    assert.equal(redirectedRelay.mutationKinds.renamedReexport, 1);
    assert.equal(redirectedRelay.mutatedDifferent, 1);
    if (start === 571 || start === 763) assert.equal(redirectedRelay.mutatedVerdicts.unknown, 1);
    else
      assert.equal(
        redirectedRelay.mutatedVerdicts.unsafe,
        1,
        JSON.stringify({ start, unknown: redirectedRelay.unknownCases })
      );
    if (start === 763) {
      assert.equal(redirectedRelay.overflowIntermediateCases, 1);
      assert.match(redirectedRelay.unknownCases[0].detail, /numeric bounds/);
      const difference = redirectedRelay.unexplainedDifferences[0];
      const observed = JSON.parse(
        await readFile(join(process.env.TEST_TMPDIR, difference.artifactPath, "node.json"), "utf8")
      );
      assert.deepEqual(observed.before.events, [1]);
      assert.deepEqual(observed.after.events, [2]);
    }
    if ([123, 379, 443, 507, 635].includes(start)) {
      assert.deepEqual(redirectedRelay.certifiedWitnesses[0].calls, [{ name: "fuzzRelayRead", args: [0] }]);
      await access(join(process.env.TEST_TMPDIR, redirectedRelay.certifiedWitnesses[0].artifactPath, "verdict.json"));
      const witness = JSON.parse(
        await readFile(
          join(process.env.TEST_TMPDIR, redirectedRelay.certifiedWitnesses[0].artifactPath, "node-witness.json"),
          "utf8"
        )
      );
      assert.deepEqual(witness.before.results, [{ value: 1 }]);
      assert.deepEqual(witness.after.results, [{ value: 2 }]);
      const metadata = JSON.parse(
        await readFile(
          join(process.env.TEST_TMPDIR, redirectedRelay.certifiedWitnesses[0].artifactPath, "case.json"),
          "utf8"
        )
      );
      assert.equal(metadata.seed, 51981);
      assert.equal(metadata.index, start);
      assert.equal(metadata.mutationMode, "enabled");
      assert.equal(metadata.unsafeShrinkAttempts, 0);
      assert.equal(metadata.plannedMutationKind, "renamedReexport");
      assert.equal(typeof metadata.nodeVersion, "string");
    }
  }
}
if (shards === 4 && shard === 3) {
  // 848 includes isolated UTF-16 surrogates in the recorded trace.
  for (const start of [16, 848]) {
    const mutated = await runFuzz({
      seed: 51981,
      start,
      cases: 1,
      outputDir: process.env.TEST_TMPDIR,
      grammarMode: "composed",
      validateEvery: 1,
    });
    assert.equal(mutated.mutationKinds.composedEffect, 1);
    assert.equal(mutated.mutatedDifferent, 1);
    assert.equal(mutated.mutatedVerdicts.unsafe, 1);
  }
}
assert.deepEqual(makeCase(123, 42), makeCase(123, 42));
assert.notDeepEqual(makeCase(123, 42), makeCase(124, 42));
const scopedCandidate = makeCase(123, 11);
assert.ok(Object.values(scopedCandidate.spec.modules).filter((module) => module.statements.length).length > 1);
await assert.rejects(
  runFuzz({ seed: 1, cases: 1, outputDir: process.env.TEST_TMPDIR, shard: 2, shards: 2 }),
  /shard must/
);
await assert.rejects(
  runFuzz({ seed: 1, cases: 1, outputDir: process.env.TEST_TMPDIR, mutationMode: "invalid" }),
  /unknown mutation mode/
);
await assert.rejects(
  runFuzz({ seed: 1, cases: 1, outputDir: process.env.TEST_TMPDIR, unsafeShrinkAttempts: -1 }),
  /unsafe shrink attempts/
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
  const aliasOriginal = makeLocalAliasCase(157, 2);
  const independentAlias = independentSplit(aliasOriginal, 157, 2);
  assert.deepEqual(independentAlias, independentSplit(aliasOriginal, 157, 2));
  assert.equal(independentAlias.source, aliasOriginal.source);
  assert.ok(Object.values(independentAlias.spec.modules).some((module) => module.renameLocalImports));
  const aliasCampaign = await runFuzz({
    seed: 157,
    cases: 32,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    splitMode: "independent",
    validateEvery: 1000,
  });
  assert.equal(aliasCampaign.generated + aliasCampaign.independentRefused, aliasCampaign.cases);
  assert.ok(aliasCampaign.localImportAliasRequestedCases > 0);
  assert.ok(aliasCampaign.localImportAliasGeneratedCases > 0);
  assert.ok(aliasCampaign.localImportAliasComparedCases > 0);
  assert.ok(aliasCampaign.parameterizedAliasComparedCases > 0);
  assert.ok(aliasCampaign.parameterizedAliasWithLocalImportComparedCases > 0);
  assert.ok(aliasCampaign.parameterizedAliasFunctionImportComparedCases > 0);
  assert.ok(aliasCampaign.refused > 0);
  const aliasProof = await runFuzz({
    seed: 157,
    start: 29,
    cases: 1,
    outputDir: process.env.TEST_TMPDIR,
    grammarMode: "alias",
    splitMode: "independent",
    validateEvery: 1,
  });
  assert.equal(aliasProof.parameterizedAliasFunctionImportComparedCases, 1);
  assert.equal(aliasProof.parameterizedAliasProvedCases, 1);
  assert.equal(aliasProof.equal, 1);
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
  assert.equal(composed.importAliasRequestedCases, 5);
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
if (shards === 4 && shard === 0) {
  for (const start of [279, 343, 407, 471, 535]) {
    const changedThrow = await runFuzz({
      seed: 51981,
      start,
      cases: 1,
      outputDir: process.env.TEST_TMPDIR,
      grammarMode: "composed",
      validateEvery: 1,
    });
    assert.equal(changedThrow.mutationKinds.throwValue, 1);
    assert.equal(changedThrow.mutatedDifferent, 1);
    assert.equal(changedThrow.mutatedVerdicts.unsafe, 1);
  }
}
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
  for (const kind of ["binding", "effect", "function", "export", "import"]) assert.ok(bulk.mutationKinds[kind] > 0);
if (shards === 4 && shard === 0) {
  for (const start of [279, 343, 407, 471, 535]) {
    const changedThrow = await runFuzz({
      seed: 51981,
      start,
      cases: 1,
      outputDir: process.env.TEST_TMPDIR,
      grammarMode: "composed",
      validateEvery: 1,
    });
    assert.equal(changedThrow.mutationKinds.throwValue, 1);
    assert.equal(changedThrow.mutatedDifferent, 1);
    assert.equal(changedThrow.mutatedVerdicts.unsafe, 1);
  }
}
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
      .filter((item) => item.hasTextObservation)
      .map((item) => item.ast.body[1].expression.arguments[0].value)
  ),
  new Set(["start\ud800!\udc00🙂", 'quote "value"', "slash \\ path", "é 🙂\n\t\0", "👩‍💻"])
);
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
