import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { split } from "../testing/split.mjs";

const root = process.env.TEST_TMPDIR;
const source = `let trace = "";
let current = 0;
async function left() { await 0; trace += "A" + current; throw "left"; }
async function right() { await 0; trace += "B" + current; return current; }
function setValue(next) { current = next; }
const first = left();
const second = right();
setValue(7);
const inTurn = trace;
`;

for (const [mode, trace] of [
  ["preserve", "A7B7"],
  ["swap", "B7A7"],
  ["abrupt", "A7B7"],
]) {
  const input = path.join(root, `${mode}.mjs`);
  const out = path.join(root, `${mode}-output`);
  const abrupt = mode === "abrupt";
  writeFileSync(input, source + (abrupt ? 'throw "stop";\n' : "") + "export { first, second, trace, inTurn };\n");
  const observed = JSON.parse(execFileSync(process.env.AWAIT_STAGED_LINK, [input, out, mode], { encoding: "utf8" }));
  assert.deepEqual(observed, {
    trace,
    swapped: mode === "swap",
    abrupt,
    exports: ["first", "second", "trace", "inTurn"],
    pending: 2,
  });
  assert.equal(existsSync(out), false);
}

const importedCapture = path.join(root, "imported-capture.mjs");
const importedCaptureOutput = path.join(root, "imported-capture-output");
writeFileSync(importedCapture, source + "export { first, second, trace, inTurn };\n");
assert.deepEqual(
  JSON.parse(
    execFileSync(process.env.AWAIT_STAGED_LINK, [importedCapture, importedCaptureOutput, "imported-capture"], {
      encoding: "utf8",
    })
  ),
  { importedCaptureRefused: true }
);
assert.equal(existsSync(importedCaptureOutput), false);

// Exercise the normal CLI on the same source and both proposed placements.
const input = path.join(root, "publish.mjs");
writeFileSync(input, source + "export { first, second, trace, inTurn };\n");
function authored(name, unitIds) {
  const spec = path.join(root, name + "-spec.json");
  writeFileSync(
    spec,
    JSON.stringify({
      entry: "entry.mjs",
      modules: {
        "worker.mjs": [0, 1, 2, 3, 4].map((id) => "#" + id),
        "entry.mjs": unitIds.map((id) => "#" + id),
      },
      order: ["worker.mjs"],
      sequential: true,
    })
  );
  return spec;
}

const spec = authored("preserve", [5, 6, 7, 8, 9]);
const out = path.join(root, "published");
const request = { source: input, spec, out, require: "all-outcomes" };
const published = split(request);
assert.equal(published.status, "certified", published.log);
assert.equal(published.scope, "two-job-primitive-await", published.log);
assert.equal(published.guarantee, "all-outcomes", published.log);
assert.deepEqual(published.files, ["entry.mjs", "worker.mjs"]);

const rechecked = split({ ...request, checkOutput: true });
assert.equal(rechecked.status, "certified", rechecked.log);
const exported = await import(pathToFileURL(path.join(out, "entry.mjs")).href);
assert.equal(exported.inTurn, "");
assert.deepEqual(await Promise.allSettled([exported.first, exported.second]), [
  { status: "rejected", reason: "left" },
  { status: "fulfilled", value: 7 },
]);
assert.equal(exported.trace, "A7B7");

const swapped = authored("swapped", [6, 5, 7, 8, 9]);
const refused = split({ ...request, spec: swapped, out: path.join(root, "swapped-out") });
assert.equal(refused.status, "refused", refused.log);
assert.match(refused.reason, /different ordered linked units or bindings/, refused.log);

const oldOut = path.join(root, "existing");
mkdirSync(oldOut);
writeFileSync(path.join(oldOut, "keep.txt"), "existing destination\n");
const untouched = split({ ...request, spec: swapped, out: oldOut, allowExistingOut: true });
assert.equal(untouched.status, "refused", untouched.log);
assert.match(untouched.reason, /different ordered linked units or bindings/, untouched.log);

const tamperedOut = path.join(root, "tampered");
cpSync(out, tamperedOut, { recursive: true });
const tamperedEntry = path.join(tamperedOut, "entry.mjs");
const originalEntry = readFileSync(tamperedEntry, "utf8");
const changedEntry = originalEntry.replace("setValue(7)", "setValue(8)");
assert.notEqual(changedEntry, originalEntry);
writeFileSync(tamperedEntry, changedEntry);
const tampered = split({ ...request, out: tamperedOut, checkOutput: true });
assert.equal(tampered.status, "refused", tampered.log);
