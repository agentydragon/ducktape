import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
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
assert.deepEqual(row.emission, { scope: "retained-tail", guarantee: "normal-completion" });
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

// A stronger guarantee must not silently reuse the normal-completion proof.
const stronger = path.join(stage, "stronger");
const refuse = run(process.env.LOWERSPLIT, [manifest, stronger, "--require", "all-outcomes"]);
assert.equal(refuse.status, 1, refuse.stdout + refuse.stderr);
assert.equal(existsSync(stronger), false);

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
const failed = run(process.env.LOWERSPLIT, [manifest, published, "--check-output"]);
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
const wrongOwner = run(process.env.LOWERSPLIT, [wrongManifest, absent]);
assert.equal(wrongOwner.status, 1, wrongOwner.stdout + wrongOwner.stderr);
assert.equal(existsSync(absent), false);
