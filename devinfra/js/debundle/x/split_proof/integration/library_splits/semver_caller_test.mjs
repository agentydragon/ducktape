import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [source] = process.argv.slice(2);
const text = readFileSync(source, "utf8");
assert.equal(
  createHash("sha256").update(text).digest("hex"),
  "9a1ab71f40d915f55f3985c314a844d7686f936b1a097dd78ded2c92cf84ba44"
);
const { body } = JSON.parse(readFileSync(source.slice(0, -4) + ".json", "utf8"));
const factory = body[6];
assert.equal(factory.declarations?.[0]?.id?.name, "__commonJS");
const original = text.slice(factory.start, factory.end);
const root = process.env.TEST_TMPDIR;
const projected = path.join(root, "projected.mjs");
const staged = path.join(root, "staged");
writeFileSync(
  projected,
  [
    'var __getOwnPropNames = function (_) { return ["run"]; };',
    "var decoy = __getOwnPropNames;",
    original,
    "export function open(callback) { return __commonJS({ run: callback }); }",
    "export const alias = open;",
    "",
  ].join("\n")
);
console.log(execFileSync(process.env.SEMVER_CALLER, [source, projected, staged], { encoding: "utf8" }).trim());
assert(readFileSync(path.join(staged, "wrapper.mjs"), "utf8").includes(original));

for (const file of [projected, path.join(staged, "entry.mjs")]) {
  const { open, alias } = await import(pathToFileURL(file).href);
  assert.equal(alias, open);
  const sentinel = {};
  const visited = [];
  const read = alias(function (exports, module) {
    assert.equal(this, undefined);
    assert.equal(module.exports, exports);
    assert.equal(read(), exports);
    exports.written = true;
    visited.push(exports);
    if (visited.length === 1) throw sentinel;
  });
  assert.throws(read, (reason) => reason === sentinel);
  assert.equal(visited[0].written, true);
  const result = read();
  assert.equal(result.written, true);
  assert.equal(visited.length, 2);
  assert.notEqual(visited[0], result);
  assert.equal(read(), result);
  assert.equal(visited.length, 2);
}
