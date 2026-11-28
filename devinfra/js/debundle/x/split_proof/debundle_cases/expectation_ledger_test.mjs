import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { writeDeployment } from "../translation/testing/deployment_manifest.mjs";
import { expectationLedger, observeExpectations } from "./expectations.mjs";

const partial = { stdout: "", source: { "main/entry.js": { contains: ["value"] } }, check_all_js: true, future: true };
for (const status of ["unchecked", "refused"]) {
  const items = expectationLedger(partial, status);
  for (const name of ["source:main/entry.js", "check_all_js", "unrepresented:future"])
    assert.equal(items.find((item) => item.assertion === name).status, status);
  assert.equal(items.find((item) => item.assertion === "exports").status, "inapplicable");
}

const root = process.env.TEST_TMPDIR;
const out = path.join(root, "report.jsonl");
const keep = path.join(root, "kept");
const runner = fileURLToPath(new URL("./run_cases.mjs", import.meta.url));
const result = spawnSync(
  process.execPath,
  [runner, "--certify", "--filter", "emission/catchall_exports/unclaimed", "--out", out, "--keep", keep],
  { encoding: "utf8" }
);
assert.equal(result.status, 0, result.stdout + result.stderr);
const row = JSON.parse(readFileSync(out, "utf8").trim());
assert.equal(row.id, "emission/catchall_exports/unclaimed");
assert.equal(row.outcome, "certified");
assert.equal(row.behavior, "pass");
assert(
  row.expectations.every(({ status }) => ["pass", "inapplicable"].includes(status)),
  JSON.stringify(row)
);
const exports = row.expectations.find(({ assertion }) => assertion === "exports:main/entry.js");
assert.deepEqual(exports.detail.original, ["foo", "x-y"]);
assert.deepEqual(exports.detail.emitted, ["foo", "x-y"]);
assert.equal(row.expectations.find(({ assertion }) => assertion === "probes:read.probe.mjs").status, "pass");

// Alter only the staged copy after its successful independent disk check.
const stage = path.join(keep, row.id);
const output = path.join(stage, "certified");
const entry = path.join(output, row.entry);
const bytes = readFileSync(entry, "utf8");
const wrong = bytes.replace(/\bfoo\b/, "f00");
assert.notEqual(wrong, bytes);
writeFileSync(entry, wrong);
const run = (cmd, args, opts) => {
  const r = spawnSync(cmd, args, { encoding: "utf8", ...opts });
  return { code: r.status, stdout: r.stdout ?? "", stderr: r.stderr ?? "", failure: r.error?.message ?? null };
};
const nodeRun = (_, file, cwd) => run(process.execPath, [file], { cwd });
const accept = {
  stdout: "",
  exports: { "main/entry.js": { include: ["foo", "x-y"] } },
  probes: { "read.probe.mjs": { stdout: "1 1\n" } },
};
const observed = observeExpectations({ accept, stage, chunkFile: "main.js", output, entry: row.entry, run, nodeRun });
assert.equal(observed.find(({ assertion }) => assertion === "exports:main/entry.js").status, "fail");

// Run the saved declarator case through the actual certificate and ledger adapters.
const declaratorId = "emission/lowering/split_declarator_fragments/spec";
const declarators = run(process.execPath, [
  runner,
  "--certify",
  "--filter",
  declaratorId,
  "--out",
  out,
  "--keep",
  keep,
]);
assert.equal(declarators.code, 0, declarators.stdout + declarators.stderr);
const declarator = JSON.parse(readFileSync(out, "utf8").trim());
assert.equal(declarator.id, declaratorId);
assert.equal(declarator.outcome, "certified");
assert.deepEqual(declarator.emission, { scope: "retained-tail", guarantee: "normal-completion" });
assert.deepEqual(declarator.disk, declarator.emission);
assert.equal(declarator.behavior, "pass");
const authoredOwner = "static/app/modules/x.js";
const authoredResidual = "static/app/modules/residual/unhandled.js";
const assertion = (ledger, name) => ledger.find((item) => item.assertion === name);
assert.deepEqual(assertion(declarator.expectations, `exports:${authoredOwner}`), {
  assertion: `exports:${authoredOwner}`,
  status: "pass",
  detail: { original: ["c", "z"], emitted: ["c"], file: "x.mjs" },
});
assert.deepEqual(assertion(declarator.expectations, `exports:${authoredResidual}`), {
  assertion: `exports:${authoredResidual}`,
  status: "pass",
  detail: { original: ["c", "z"], emitted: ["a", "b", "z"], file: "residual.mjs" },
});
assert.deepEqual(assertion(declarator.expectations, "check_all_js"), {
  assertion: "check_all_js",
  status: "pass",
  detail: { files: ["entry.mjs", "residual.mjs", "x.mjs"], failures: [] },
});
const declStage = path.join(keep, declaratorId);
const declOutput = path.join(declStage, "certified");
const residual = path.join(declOutput, "residual.mjs");
const originalResidual = readFileSync(residual, "utf8");
const observe = () =>
  observeExpectations({
    accept: {
      stdout: "3\n",
      exports: {
        [authoredOwner]: { include: ["c"] },
        [authoredResidual]: { exclude: ["c"] },
      },
      check_all_js: true,
    },
    stage: declStage,
    chunkFile: "static/app.js",
    output: declOutput,
    entry: declarator.entry,
    moduleOutputs: new Map([
      [authoredOwner, "x.mjs"],
      [authoredResidual, "residual.mjs"],
    ]),
    run,
    nodeRun,
  });

// Add a second cell with the same numeric value under the wrong namespace.
// The entry's normal output and all JS syntax still pass.
writeFileSync(residual, originalResidual + "\nconst wrongC = 3; export { wrongC as c };\n");
const wrongOwner = observe();
assert.equal(assertion(wrongOwner, "stdout:emitted").status, "pass");
assert.equal(assertion(wrongOwner, `exports:${authoredOwner}`).status, "pass");
assert.equal(assertion(wrongOwner, `exports:${authoredResidual}`).status, "fail");
assert.equal(assertion(wrongOwner, "check_all_js").status, "pass");
const source = path.join(declStage, "static/app.js");
const manifest = writeDeployment(source, path.join(declStage, "certificate-spec.json"), declOutput);
const diskCheck = () => run(process.env.LOWERSPLIT, [manifest, declOutput, "--check-output"]);
const wrongNamespace = diskCheck();
assert.equal(wrongNamespace.code, 1, wrongNamespace.stdout + wrongNamespace.stderr);
assert.equal(JSON.parse(wrongNamespace.stdout).status, "rejected");

writeFileSync(residual, originalResidual);
const ownerFile = path.join(declOutput, "x.mjs");
const ownerSource = readFileSync(ownerFile, "utf8");
const probeOwner = () =>
  run(process.execPath, [
    "--input-type=module",
    "-e",
    "const m = await import(process.argv[1]); console.log(JSON.stringify([Object.keys(m), m.c]));",
    pathToFileURL(ownerFile).href,
  ]);
assert.equal(probeOwner().stdout, '[["c"],3]\n');
// Preserve both the owner's export name and its observed primitive value while
// changing the exported cell. The runtime ledger still passes; disk readback catches it.
const wrongCell = ownerSource.replace(/export\s*\{\s*c\s*\}/, "const wrongC = 3; export { wrongC as c }");
assert.notEqual(wrongCell, ownerSource);
writeFileSync(ownerFile, wrongCell);
assert.equal(probeOwner().stdout, '[["c"],3]\n');
assert(observe().every(({ status }) => ["pass", "inapplicable"].includes(status)));
const wrongBinding = diskCheck();
assert.equal(wrongBinding.code, 1, wrongBinding.stdout + wrongBinding.stderr);
assert.equal(JSON.parse(wrongBinding.stdout).status, "rejected");
writeFileSync(ownerFile, ownerSource);
const unused = path.join(declOutput, "unused.mjs");
writeFileSync(unused, "const = ;");
const badSyntax = observe();
assert.equal(assertion(badSyntax, "stdout:emitted").status, "pass");
assert.equal(assertion(badSyntax, "check_all_js").status, "fail");
assert.deepEqual(assertion(badSyntax, "check_all_js").detail.failures, ["unused.mjs"]);
