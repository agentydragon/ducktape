import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";

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
