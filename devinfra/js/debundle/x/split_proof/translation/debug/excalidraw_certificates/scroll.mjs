import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../../../lowering/dump_core.mjs";

const chunks = process.env.EXCALIDRAW_INPUTS.split(" ").find((file) => file.endsWith("/chunks"));
assert(chunks);
const authored = JSON.parse(readFileSync(process.env.EDITOR_LEAN_SPEC, "utf8"));
const scroll = "packages/excalidraw/scene/scroll.mjs";
const selected = authored.modules[scroll];
assert.deepEqual(selected, ["#1337", "#1338"]);
const selectors = Object.values(authored.modules).flat();
assert.equal(selectors.length, 1787);
const all = Array.from({ length: selectors.length }, (_, i) => "#" + i);
assert.deepEqual(new Set(selectors), new Set(all));
const { entry } = authored;
const trial = {
  entry,
  modules: { [entry]: all.filter((id) => !selected.includes(id)), [scroll]: selected },
  order: [scroll],
  sequential: true,
};
const program = lowerDump(readFileSync(path.join(chunks, "editor.js"), "utf8"), acorn, eslintScope);
const ast = path.join(process.env.TEST_TMPDIR, "editor.json");
writeFileSync(ast, JSON.stringify(program));
const spec = path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "scroll-spec.json");
writeFileSync(spec, JSON.stringify(trial, null, 2));
const out = path.join(process.env.TEST_TMPDIR, "modules");
const run = spawnSync(process.env.LOWERSPLIT, [ast, spec, out, "--require", "all-outcomes"], { encoding: "utf8" });
if (run.error) throw run.error;
assert.equal(run.signal, null);
assert.equal(run.status, 1, run.stdout + run.stderr);
const verdict = JSON.parse(run.stdout.trimEnd().split("\n").at(-1));
const first = program.body[0];
writeFileSync(
  path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, "result.json"),
  JSON.stringify(
    {
      selected,
      units: selectors.length,
      firstStatement: first.type,
      source: first.source?.lit,
      verdict,
    },
    null,
    2
  )
);
assert.equal(first.type, "ImportDeclaration");
assert.deepEqual(verdict, {
  requested: "all-outcomes",
  scope: "closed-declarations",
  status: "rejected",
  guarantee: null,
  reason: "statement 0: expected a variable declaration, got ImportDeclaration",
});
assert(!existsSync(out), "a rejected trial published modules");
