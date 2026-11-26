import assert from "node:assert/strict";
import { mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";
import { split } from "../testing/split.mjs";
import { observe, prepare, root } from "./harness.mjs";

const entry = "entry.mjs";
const worker = "worker.mjs";
const source = `const first = 42;
const second = 41;
export const value = first;
`;
const spec = { entry, modules: { [entry]: ["value"], [worker]: ["first", "second"] } };
const files = prepare(root, { source, provider: "", spec });
const out = path.join(root, "out");
const run = (options = {}) =>
  split({ source: files.sourceFile, spec: files.specFile, out, require: "all-outcomes", ...options });
const snapshot = () =>
  readdirSync(out)
    .sort()
    .map((name) => [name, readFileSync(path.join(out, name))]);

const emitted = run();
assert.equal(emitted.status, "certified", emitted.log);
assert.equal(emitted.verdict.scope, "checked-construction", emitted.log);
assert.equal(emitted.verdict.strategy, "compiled-constructor", emitted.log);
assert.equal(emitted.verdict.guarantee, "all-outcomes", emitted.log);
assert.equal(emitted.verdict.every_ending_matches, true, emitted.log);
assert.equal(emitted.verdict.observation_scope, "strict-static-data", emitted.log);
assert.match(emitted.verdict.target_theorem, /CoreJS world/);
assert.deepEqual(emitted.verdict.premises, []);
assert.deepEqual(emitted.files, [entry, worker]);
assert.deepEqual(observe(path.join(out, entry), "value"), { value: 42 });

const reread = run({ checkOutput: true });
assert.deepEqual(reread.verdict, emitted.verdict);

const phases = (stderr) =>
  stderr
    .trim()
    .split("\n")
    .filter((line) => line.includes('"phase":'))
    .map((line) => JSON.parse(line).phase);
assert(phases(emitted.stderr).includes("checked_construction"), emitted.log);
assert(phases(emitted.stderr).includes("checked_correspondence"), emitted.log);
assert(!phases(emitted.stderr).includes("certificate_preparation"), emitted.log);
assert(!phases(reread.stderr).includes("output_publication"), reread.log);

const entryFile = path.join(out, entry);
const workerFile = path.join(out, worker);
const originalEntry = readFileSync(entryFile);
writeFileSync(entryFile, `import { second as first } from "./${worker}";\nexport const value = first;\n`);
const beforeWrongCell = snapshot();
const wrongCell = run({ checkOutput: true, allowExistingOut: true });
assert.equal(wrongCell.status, "refused", wrongCell.log);
assert.match(wrongCell.verdict.reason, /output module entry\.mjs differs from its generated ESTree/);
assert.deepEqual(snapshot(), beforeWrongCell, "wrong-cell check modified the existing output");
writeFileSync(entryFile, originalEntry);

const wrongModule = path.join(out, "renamed.mjs");
renameSync(workerFile, wrongModule);
const beforeWrongModule = snapshot();
const renamed = run({ checkOutput: true, allowExistingOut: true });
assert.equal(renamed.status, "refused", renamed.log);
assert.match(renamed.verdict.reason, /output module set differs/);
assert.deepEqual(snapshot(), beforeWrongModule, "wrong-module check modified the existing output");
renameSync(wrongModule, workerFile);

const reorderedSpecFile = path.join(root, "reordered-spec.json");
writeFileSync(
  reorderedSpecFile,
  JSON.stringify({
    entry,
    order: [worker],
    sequential: true,
    modules: { [entry]: ["first", "value"], [worker]: ["second"] },
  })
);
const beforeUnsupported = snapshot();
const reordered = run({ spec: reorderedSpecFile, allowExistingOut: true });
assert.equal(reordered.status, "refused", reordered.log);
assert.equal(reordered.verdict.strategy, "compiled-constructor", reordered.log);
assert.equal(reordered.verdict.reason_code, "constructor_refusal", reordered.log);
assert.match(reordered.verdict.reason, /changes the source unit order/);
assert.deepEqual(snapshot(), beforeUnsupported, "unsupported move modified the existing output");

const callableSource = `function read() { return 42; }\nexport const value = read();\n`;
const callable = prepare(path.join(root, "callable"), {
  source: callableSource,
  provider: "",
  spec: { entry, modules: { [entry]: ["value"], [worker]: ["read"] } },
});
const browserContract = path.join(root, "callable-browser-contract.json");
writeFileSync(browserContract, JSON.stringify({ profile: "browser-page" }));
for (const [name, options, existingOutput] of [
  ["default", {}, false],
  ["structural", { structural: true }, true],
  ["profile", { hostContract: browserContract }, false],
]) {
  const callableOut = path.join(root, `callable-${name}-out`);
  const existingOutputFile = path.join(callableOut, entry);
  const existingBytes = existingOutput ? Buffer.from("existing output\n") : null;
  if (existingBytes) {
    mkdirSync(callableOut, { recursive: true });
    writeFileSync(existingOutputFile, existingBytes);
  }
  const callableResult = split({
    source: callable.sourceFile,
    spec: callable.specFile,
    out: callableOut,
    allowExistingOut: existingOutput,
    ...options,
  });
  assert.equal(callableResult.status, "refused", callableResult.log);
  assert.equal(callableResult.verdict.scope, "checked-construction", callableResult.log);
  assert.equal(callableResult.verdict.requested, "normal-completion", callableResult.log);
  assert.equal(callableResult.verdict.guarantee, null, callableResult.log);
  assert.equal(callableResult.verdict.every_ending_matches, false, callableResult.log);
  assert.equal(callableResult.verdict.reason_code, "strict_observation_unsupported", callableResult.log);
  assert.equal(callableResult.verdict.strategy, "compiled-constructor", callableResult.log);
  if (existingBytes) {
    assert.deepEqual(readFileSync(existingOutputFile), existingBytes, "structural refusal changed prior output bytes");
    assert.deepEqual(readdirSync(callableOut), [entry], "structural refusal changed prior output files");
  }
}

// The source and split use the same entry URL in fresh Node processes. Moving
// read changes the first stack frame from entry.mjs to worker.mjs.
const stackRoot = path.join(root, "same-entry-url");
mkdirSync(stackRoot, { recursive: true });
const stackEntry = path.join(stackRoot, entry);
const stackWorker = path.join(stackRoot, worker);
const stackSource = `function read() { return new Error("audit").stack; }\nexport const value = read();\n`;
const runAtSameUrl = () => observe(stackEntry, "value").value;
writeFileSync(stackEntry, stackSource);
const originalStack = runAtSameUrl();
writeFileSync(stackWorker, `function read() { return new Error("audit").stack; }\nexport { read };\n`);
writeFileSync(stackEntry, `import { read } from "./${worker}";\nexport const value = read();\n`);
const movedStack = runAtSameUrl();
assert.match(originalStack, /entry\.mjs:1:26/);
assert.match(movedStack, /worker\.mjs:1:26/);
assert.notEqual(movedStack, originalStack);

const stackFixture = prepare(path.join(root, "stack-source"), {
  source: stackSource,
  provider: "",
  spec: { entry, modules: { [entry]: ["value"], [worker]: ["read"] } },
});
for (const [name, options] of [
  ["structural", { structural: true }],
  ["profiled-structural", { structural: true, hostContract: browserContract }],
]) {
  const stackOut = path.join(root, `stack-${name}-out`);
  const sentinel = path.join(stackOut, entry);
  const priorBytes = Buffer.from("keep prior output\n");
  mkdirSync(stackOut, { recursive: true });
  writeFileSync(sentinel, priorBytes);
  const refused = split({
    source: stackFixture.sourceFile,
    spec: stackFixture.specFile,
    out: stackOut,
    require: "all-outcomes",
    allowExistingOut: true,
    ...options,
  });
  assert.equal(refused.status, "refused", refused.log);
  assert.equal(refused.verdict.guarantee, null, refused.log);
  assert.equal(refused.verdict.every_ending_matches, false, refused.log);
  assert.equal(refused.verdict.reason_code, "strict_observation_unsupported", refused.log);
  assert.deepEqual(readFileSync(sentinel), priorBytes, "stack-observation refusal changed prior output bytes");
  assert.deepEqual(readdirSync(stackOut), [entry], "stack-observation refusal changed prior output files");
}

const pureSparseSource = `export const value = [1, , 2];\n`;
const pureSparse = prepare(path.join(root, "pure-sparse"), {
  source: pureSparseSource,
  provider: "",
  spec: { entry, modules: { [entry]: ["value"] } },
});
const pureSparseOut = path.join(root, "pure-sparse-out");
const pureSparseResult = split({
  source: pureSparse.sourceFile,
  spec: pureSparse.specFile,
  out: pureSparseOut,
  require: "all-outcomes",
});
assert.equal(pureSparseResult.status, "certified", pureSparseResult.log);
assert.deepEqual(observe(path.join(pureSparseOut, entry), "value"), observe(pureSparse.sourceFile, "value"));

assert.deepEqual(snapshot(), beforeUnsupported, "all refusal paths must preserve the previously published files");
