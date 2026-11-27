import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { certify } from "./certify.mjs";

const tmp = process.env.TEST_TMPDIR;
const out = path.join(tmp, "results.jsonl");
const id = "emission/catchall_member_rename/spec";
execFileSync(
  process.execPath,
  [fileURLToPath(new URL("./run_cases.mjs", import.meta.url)), "--certify", "--filter", id, "--out", out],
  { encoding: "utf8" }
);
const [record] = readFileSync(out, "utf8").trim().split("\n").map(JSON.parse);
const rename = { ordinal: 0, owner: "residual/unhandled", binding: "a", readable: "FirstFn" };
assert.equal(record.id, id);
assert.equal(record.outcome, "rejected");
assert.equal(record.emission.code, "untranslated_member_rename");
assert.deepEqual(record.emission.renames, [rename]);

// Existing outputs with the right return values can still have the wrong
// names or export the wrong function object. Refusal must not replace them.
for (const wrong of ["wrong-name", "wrong-cell"]) {
  const stage = path.join(tmp, wrong);
  const tree = path.join(stage, "certified");
  const residual = path.join(tree, "residual_unhandled.mjs");
  const entry = path.join(tree, "entry.mjs");
  mkdirSync(tree, { recursive: true });
  const fn = wrong === "wrong-name" ? "a" : "FirstFn";
  writeFileSync(residual, `export function ${fn}() { return 1; }\nexport function b() { return 2; }\n`);
  writeFileSync(
    entry,
    wrong === "wrong-name"
      ? 'export { a, b } from "./residual_unhandled.mjs";\n'
      : 'export { FirstFn, b } from "./residual_unhandled.mjs";\nexport function a() { return 1; }\n'
  );
  const api = await import(pathToFileURL(entry).href);
  const helpers = await import(pathToFileURL(residual).href);
  assert.equal(api.a(), 1);
  assert.equal(api.b(), 2);
  if (wrong === "wrong-name") {
    assert.deepEqual(Object.keys(helpers), ["a", "b"]);
  } else {
    assert.deepEqual(Object.keys(helpers), ["FirstFn", "b"]);
    assert.notEqual(api.a, helpers.FirstFn);
  }
  const snapshot = () => [entry, residual].map((file) => [readFileSync(file), statSync(file).mtimeMs]);
  const before = snapshot();
  const result = certify({
    stage,
    resolvedUnits: [{ ordinal: rename.ordinal, owner: rename.owner, members: { [rename.binding]: rename.readable } }],
    run: () => assert.fail("cannot run the ordinary CLI with a dropped member rename"),
  });
  assert.deepEqual(result.emission, record.emission);
  assert.deepEqual(snapshot(), before);
}
console.log(JSON.stringify(record));
