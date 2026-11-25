// The footprint scan treats an object published by an export as world-visible,
// while a private object stays local to the module.
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import path from "node:path";
import { dump } from "./parser.mjs";

const temp = process.env.TEST_TMPDIR;
assert(temp);

function footprints(name, code) {
  const input = path.join(temp, `${name}-ast.json`);
  writeFileSync(input, JSON.stringify(dump(code)));
  const lower = spawnSync(process.env.LOWER, [input], { encoding: "utf8" });
  assert.equal(lower.status, 0, lower.stderr);
  const effects = lower.stdout
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line).effect);

  return effects;
}

const exported = footprints("exported", "const o = { n: 0 };\no.n = 1;\nexport { o };\n");
assert.equal(exported[1], true, "a store into an exported object touches the world");

const exportedDeclaration = footprints("exported_declaration", "export const o = { n: 0 };\no.n = 1;\n");
assert.equal(exportedDeclaration[1], true, "`export const o` publishes o");

const private_ = footprints("private", "const o = { n: 0 };\no.n = 1;\n");
assert.equal(private_[1], false, "a store into a private object stays inside the module");
