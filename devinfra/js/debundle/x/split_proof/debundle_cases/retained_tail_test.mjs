import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { acorn } from "../translation/parser.mjs";
import { writeDeployment } from "../translation/testing/deployment_manifest.mjs";

const id = "realizability/comma_list_split_does_not_invent_cross_module_cycle/spec";
const root = process.env.TEST_TMPDIR;
const report = path.join(root, "report.jsonl");
const keep = path.join(root, "kept");
function run(binary, args) {
  const result = spawnSync(binary, args, { encoding: "utf8" });
  assert.ifError(result.error);
  return result;
}

const runner = fileURLToPath(new URL("./run_cases.mjs", import.meta.url));
const result = run(process.execPath, [runner, "--certify", "--filter", id, "--out", report, "--keep", keep]);
assert.equal(result.status, 0, result.stdout + result.stderr);
const row = JSON.parse(readFileSync(report, "utf8"));
assert.equal(row.id, id);
assert.equal(row.outcome, "certified");
assert.deepEqual(row.emission, { scope: "retained-tail", guarantee: "all-outcomes" });
assert.deepEqual(row.disk, row.emission);
assert.equal(row.behavior, "pass");
assert.deepEqual(
  row.expectations.filter(({ status }) => status === "pass").map(({ assertion }) => assertion),
  ["stdout:original", "stdout:emitted", "entry"]
);

const stage = path.join(keep, id);
const request = path.join(stage, "certificate-spec.json");
const source = path.join(stage, "static/app.js");
const published = path.join(stage, "certified");
const emittedEntry = path.join(published, row.entry);
const manifest = writeDeployment(source, request, published);
const readback = run(process.env.LOWERSPLIT, [manifest, published, "--check-output"]);
assert.equal(readback.status, 0, readback.stdout + readback.stderr);

const worldTest = (original, spec, output, firstCapture) => {
  const result = run(process.env.RETAINED_TAIL_WORLD_TEST, [original, spec, output, firstCapture]);
  assert.equal(result.status, 0, result.stdout + result.stderr);
};
worldTest(source, request, published, "1");

const fragment = "emission/lowering/split_declarator_fragments/spec";
const second = run(process.execPath, [runner, "--certify", "--filter", fragment, "--out", report, "--keep", keep]);
assert.equal(second.status, 0, second.stdout + second.stderr);
const fragmentRow = JSON.parse(readFileSync(report, "utf8"));
assert.equal(fragmentRow.id, fragment);
assert.equal(fragmentRow.outcome, "certified");
assert.deepEqual(fragmentRow.emission, row.emission);
assert.deepEqual(fragmentRow.disk, row.emission);
const fragmentStage = path.join(keep, fragment);
worldTest(
  path.join(fragmentStage, "static/app.js"),
  path.join(fragmentStage, "certificate-spec.json"),
  path.join(fragmentStage, "certified"),
  "3"
);

// Exercise the explicit stronger request on both saved deployments.
for (const [dir, input, spec] of [
  [stage, source, request],
  [fragmentStage, path.join(fragmentStage, "static/app.js"), path.join(fragmentStage, "certificate-spec.json")],
]) {
  const strong = path.join(dir, "all-outcomes");
  const deployment = writeDeployment(input, spec, strong);
  const requestStrong = run(process.env.LOWERSPLIT, [deployment, strong, "--require", "all-outcomes"]);
  assert.equal(requestStrong.status, 0, requestStrong.stdout + requestStrong.stderr);
  const rereadStrong = run(process.env.LOWERSPLIT, [deployment, strong, "--require", "all-outcomes", "--check-output"]);
  assert.equal(rereadStrong.status, 0, rereadStrong.stdout + rereadStrong.stderr);
  for (const result of [requestStrong, rereadStrong]) {
    const verdict = JSON.parse(result.stdout);
    assert.equal(verdict.guarantee, "all-outcomes");
    assert.equal(verdict.every_ending_matches, true);
  }
}

// Retaining the statement's shape without its original argument is not enough.
const text = readFileSync(emittedEntry, "utf8");
const ast = acorn.parse(text, { ecmaVersion: "latest", sourceType: "module" });
const tail = ast.body.find((node) => node.type === "ExpressionStatement");
const argument = tail.expression.arguments[0];
assert.equal(argument.name, "A");
const wrong = text.slice(0, argument.start) + "B" + text.slice(argument.end);
writeFileSync(emittedEntry, wrong);
const mutatedRun = run(process.execPath, [emittedEntry]);
assert.equal(mutatedRun.status, 0, mutatedRun.stderr);
assert.equal(mutatedRun.stdout, "42 42 42 x\n");
const failed = run(process.env.LOWERSPLIT, [manifest, published, "--require", "all-outcomes", "--check-output"]);
assert.equal(failed.status, 1, failed.stdout + failed.stderr);
assert.equal(JSON.parse(failed.stdout).status, "rejected");
assert.equal(readFileSync(emittedEntry, "utf8"), wrong);

// The exact graph checks the const reads, even if an owner permutation has
// the same module names, ordinal coverage, and declaration/operation grammar.
const chosen = JSON.parse(readFileSync(request, "utf8"));
const [left, right] = chosen.order;
assert(chosen.modules[left].includes("#0") && chosen.modules[right].includes("#3"));
// Move B out of X's module. The resulting cycle evaluates B in the
// entry's first dependency before the X cell is initialized.
chosen.modules[left].push("#3");
chosen.modules[right] = chosen.modules[right].filter((selector) => selector !== "#3");
const wrongSpec = path.join(stage, "wrong-spec.json");
writeFileSync(wrongSpec, JSON.stringify(chosen));
const absent = path.join(stage, "wrong-placement");
const wrongManifest = writeDeployment(source, wrongSpec, absent);
const wrongOwner = run(process.env.LOWERSPLIT, [wrongManifest, absent, "--require", "all-outcomes"]);
assert.equal(wrongOwner.status, 1, wrongOwner.stdout + wrongOwner.stderr);
assert.equal(existsSync(absent), false);

const publicCase = "emission/return_object_global/spec";
const publicResult = run(process.execPath, [
  runner,
  "--certify",
  "--filter",
  publicCase,
  "--out",
  report,
  "--keep",
  keep,
]);
assert.equal(publicResult.status, 0, publicResult.stdout + publicResult.stderr);
const publicRow = JSON.parse(readFileSync(report, "utf8"));
assert.equal(publicRow.id, publicCase);
assert.equal(publicRow.outcome, "certified", JSON.stringify(publicRow));
assert.deepEqual(publicRow.emission, { scope: "retained-public-function", guarantee: "all-outcomes" });
assert.deepEqual(publicRow.disk, publicRow.emission);
assert.equal(publicRow.behavior, "pass");
assert.deepEqual(
  publicRow.expectations.filter(({ status }) => status === "pass").map(({ assertion }) => assertion),
  ["stdout:original", "stdout:emitted"]
);

const publicStage = path.join(keep, publicCase);
const publicSource = path.join(publicStage, "main.js");
const publicSpec = path.join(publicStage, "certificate-spec.json");
const publicOutput = path.join(publicStage, "all-outcomes");
const publicManifest = writeDeployment(publicSource, publicSpec, publicOutput);
const publicArgs = [publicManifest, publicOutput, "--require", "all-outcomes"];
for (const args of [publicArgs, [...publicArgs, "--check-output"]]) {
  const checked = run(process.env.LOWERSPLIT, args);
  assert.equal(checked.status, 0, checked.stdout + checked.stderr);
  assert.equal(JSON.parse(checked.stdout).every_ending_matches, true);
}
worldTest(publicSource, publicSpec, publicOutput, "factory");

const publicEntry = path.join(publicOutput, publicRow.entry);
const factoryModule = path.join(publicOutput, "factory.mjs");
const observePublic = (entry, ...owners) => {
  const observed = run(process.execPath, [
    "--input-type=module",
    "-e",
    `
      import assert from "node:assert/strict";
      const [entry, owner] = process.argv.slice(1);
      const log = [];
      const first = { log(value) { assert.equal(this, first); log.push(value); } };
      globalThis.console = first;
      const { factory } = await import(entry);
      assert.deepEqual(log, [true]);
      assert.equal(factory.name, "factory");
      assert.equal(factory().logger, first);
      assert.equal(new factory().logger, first);
      if (owner) assert.equal(factory, (await import(owner)).factory);
      const second = { log() { assert.fail("unexpected log"); } };
      globalThis.console = second;
      assert.equal(factory().logger, second);
      const sentinel = {};
      globalThis.console = { log(value) { log.push(value); throw sentinel; } };
      await assert.rejects(import(entry + "?throw"), (error) => error === sentinel);
      assert.deepEqual(log, [true, true]);
    `,
    ...[entry, ...owners].map((file) => pathToFileURL(file).href),
  ]);
  assert.equal(observed.status, 0, observed.stdout + observed.stderr);
};
observePublic(publicSource);
observePublic(publicEntry, factoryModule);

// The namespace still hands out the same function object and passes the Node
// probes, but it now resolves to a new binding cell instead of the import.
const entryText = readFileSync(publicEntry, "utf8");
const exportRow = acorn
  .parse(entryText, { ecmaVersion: "latest", sourceType: "module" })
  .body.find((node) => node.type === "ExportNamedDeclaration");
assert.equal(exportRow.source, null);
assert.deepEqual(
  exportRow.specifiers.map((row) => [row.local.name, row.exported.name]),
  [["factory", "factory"]]
);
const alias =
  entryText.slice(0, exportRow.start) +
  "const snapshot = factory; export { snapshot as factory };" +
  entryText.slice(exportRow.end);
writeFileSync(publicEntry, alias);
observePublic(publicEntry, factoryModule);
const wrongCell = run(process.env.LOWERSPLIT, [...publicArgs, "--check-output"]);
assert.equal(wrongCell.status, 1, wrongCell.stdout + wrongCell.stderr);
assert.equal(JSON.parse(wrongCell.stdout).status, "rejected");
assert.equal(readFileSync(publicEntry, "utf8"), alias);
