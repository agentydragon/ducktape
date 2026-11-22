// `lower` and `lowersplit` analyze an ESTree dump the same way: the names its `export` statements publish start out
// held by the world, so a unit that changes an exported object touches the world, and one that changes a private
// object does not. `lowersplit --footprints` reads what `lower` printed, so a disagreement would make the two give
// different verdicts for one bundle.
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const temp = process.env.TEST_TMPDIR;
assert(temp);

function footprints(name, code) {
  const input = path.join(temp, `${name}-ast.json`);
  writeFileSync(input, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  const lower = spawnSync(process.env.LOWER, [input], { encoding: "utf8" });
  assert.equal(lower.status, 0, lower.stderr);
  const effects = lower.stdout
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line).effect);

  const parsed = acorn.parse(code, { sourceType: "module", ecmaVersion: "latest" });
  const spec = path.join(temp, `${name}-spec.json`);
  writeFileSync(
    spec,
    JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": parsed.body.map((_, i) => `#${i}`) } })
  );
  const out = path.join(temp, name);
  mkdirSync(out, { recursive: true });
  const split = spawnSync(process.env.LOWERSPLIT, [input, spec, out], { encoding: "utf8" });
  assert.equal(split.status, 0, `${split.stdout}\n${split.stderr}`);
  const counted = Number(split.stdout.match(/(\d+) touch the world/)?.[1]);
  return { effects, counted };
}

const exported = footprints("exported", "const o = { n: 0 };\no.n = 1;\nexport { o };\n");
assert.equal(exported.effects[1], true, "a store into an exported object touches the world");
assert.equal(
  exported.counted,
  exported.effects.filter(Boolean).length,
  "lower and lowersplit agree on an exported object"
);

const exportedDeclaration = footprints("exported_declaration", "export const o = { n: 0 };\no.n = 1;\n");
assert.equal(exportedDeclaration.effects[1], true, "`export const o` publishes o");
assert.equal(exportedDeclaration.counted, exportedDeclaration.effects.filter(Boolean).length);

const private_ = footprints("private", "const o = { n: 0 };\no.n = 1;\n");
assert.equal(private_.effects[1], false, "a store into a private object stays inside the module");
assert.equal(private_.counted, 0);
