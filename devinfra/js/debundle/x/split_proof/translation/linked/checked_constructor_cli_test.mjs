import assert from "node:assert/strict";
import { readFileSync, readdirSync, renameSync, writeFileSync } from "node:fs";
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
for (const [name, options] of [
  ["default", {}],
  ["structural", { structural: true }],
  ["profile", { hostContract: browserContract }],
]) {
  const callableResult = split({
    source: callable.sourceFile,
    spec: callable.specFile,
    out: path.join(root, `callable-${name}-out`),
    ...options,
  });
  assert.equal(callableResult.status, "refused", callableResult.log);
  assert.equal(callableResult.verdict.reason_code, "strict_observation_unsupported", callableResult.log);
  assert.equal(callableResult.verdict.strategy, "compiled-constructor", callableResult.log);
}

assert.deepEqual(snapshot(), beforeUnsupported, "all refusal paths must preserve the previously published files");
