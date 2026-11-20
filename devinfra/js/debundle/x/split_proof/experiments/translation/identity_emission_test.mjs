// The old emitter rejects this move, so --force is used only to produce a
// provisional tree. The inferred translation verdict is mandatory before Node.
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";

const here = path.dirname(fileURLToPath(import.meta.url));
const temp = process.env.TEST_TMPDIR;
assert(temp, "TEST_TMPDIR is required");
const source = path.join(here, "fixtures/local_identity/bundle.js");
const code = readFileSync(source, "utf8");
const out = path.join(temp, "emitted");
mkdirSync(out, { recursive: true });
const statements = acorn.parse(code, { sourceType: "module", ecmaVersion: "latest" }).body;
const ast = path.join(temp, "ast.json");
const spec = path.join(temp, "spec.json");
const bundle = path.join(temp, "bundle.json");
const tree = path.join(temp, "tree.json");
const cert = path.join(temp, "cert.json");
writeFileSync(
  ast,
  JSON.stringify({ statements: statements.map((s) => ({ ast: s, src: code.slice(s.start, s.end) })) })
);
writeFileSync(
  spec,
  JSON.stringify({ entry: "entry.mjs", modules: { "entry.mjs": ["identity", "result", "#3"], "side.mjs": ["#2"] } })
);
const run = (bin, args) => spawnSync(bin, args, { encoding: "utf8" });
const forced = run(process.env.LOWERSPLIT, [ast, spec, out, "--force"]);
assert.equal(forced.status, 1, `expected provisional emitter rejection: ${forced.stdout}\n${forced.stderr}`);
assert.match(forced.stdout, /FAIL\s+OrderOk/);
const frontend = path.join(here, "frontend.mjs");
for (const args of [
  [frontend, "bundle", source, "--out", bundle],
  [frontend, "tree", out, "--entry", "entry.mjs", "--out", tree],
  [path.join(here, "pair.mjs"), bundle, tree, "--out", cert],
]) {
  const p = run(process.execPath, args);
  assert.equal(p.status, 0, p.stderr);
}
const checked = run(process.env.TRANSLATE, [bundle, tree, cert]);
assert.equal(checked.status, 0, `${checked.stdout}\n${checked.stderr}`);
assert.match(checked.stdout, /inferred identity call/);
assert.match(checked.stdout, /PLAN SOUND/);
const before = run(process.execPath, [source]);
const after = run(process.execPath, [path.join(out, "entry.mjs")]);
assert.equal(before.status, 0, before.stderr);
assert.equal(after.status, 0, after.stderr);
assert.equal(after.stdout, before.stdout);
