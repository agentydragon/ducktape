import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const root = process.env.TEST_TMPDIR;
const input = path.join(root, "input.json");
const placement = path.join(root, "placement.json");
function prepare(source, modules, order) {
  const ast = lowerDump(source, acorn, eslintScope);
  fs.writeFileSync(input, JSON.stringify(ast));
  fs.writeFileSync(placement, JSON.stringify({ entry: "entry.mjs", order, sequential: true, modules }));
  return ast;
}
function check(out, requested, status, ...args) {
  const result = spawnSync(process.env.LOWERSPLIT, [input, placement, out, "--require", requested, ...args], {
    encoding: "utf8",
  });
  if (result.error) throw result.error;
  const diagnostics = result.stdout + result.stderr;
  assert.equal(result.status, status === "certified" ? 0 : 1, diagnostics);
  const verdict = JSON.parse(result.stdout.trim().split("\n").at(-1));
  assert.equal(verdict.requested, requested);
  assert.equal(verdict.status, status, diagnostics);
  if (status === "rejected") {
    assert.equal(verdict.guarantee, null);
    assert(verdict.reason, diagnostics);
  }
  return verdict;
}
async function observe(url) {
  try {
    return Object.entries(await import(url));
  } catch (e) {
    return e.name;
  }
}
const all = "all-outcomes";
const normal = "normal-completion";
const entry = "entry.mjs";
const left = "left.mjs";
const right = "right.mjs";
const source = "const a = 1; const b = 2; export const result = a;";
const out = path.join(root, "mutations");
const placement3 = { [entry]: ["#2"], [left]: ["#0"], [right]: ["#1"] };
const ast = prepare(source, placement3, [left, right]);
assert.equal(check(out, all, "certified").guarantee, all);
const entryFile = path.join(out, entry);
const emitted = fs.readFileSync(entryFile, "utf8");
const imports = emitted.split("\n").filter((line) => line.startsWith("import "));
assert.equal(imports.length, 2, emitted);
for (const changed of [
  emitted.replace("result = a", "result = 0"),
  emitted.replace("export const", "const"),
  emitted.replace("result =", "different ="),
  emitted.replace("./left.mjs", "./right.mjs"),
  emitted.replace(/\ba\b/, "a as shadow"),
  emitted.replace(imports.join("\n"), imports.toReversed().join("\n")),
  emitted + "\nconst extra = 1;\n",
  emitted + "\nexport {",
]) {
  assert.notEqual(changed, emitted);
  fs.writeFileSync(entryFile, changed);
  check(out, all, "rejected", "--check-output");
}
fs.writeFileSync(entryFile, "// harmless formatting change\n" + emitted);
check(out, all, "certified", "--check-output");
const extra = path.join(out, "extra.mjs");
fs.writeFileSync(extra, "");
check(out, all, "rejected", "--check-output");
fs.unlinkSync(extra);
fs.renameSync(path.join(out, left), extra);
check(out, all, "rejected", "--check-output");
fs.renameSync(extra, path.join(out, left));
// Publication must leave a previously populated destination intact on failure.
const sentinel = path.join(out, "unrelated");
fs.writeFileSync(sentinel, "keep");
check(out, all, "rejected");
assert.equal(fs.readFileSync(sentinel, "utf8"), "keep");
fs.unlinkSync(sentinel);
ast.sourceText = source.replace("= 1", "= 3");
fs.writeFileSync(input, JSON.stringify(ast));
assert.match(check(out, all, "rejected", "--check-output").reason, /fresh parse/);

// Compose declarations, unary expressions, dependency chains and partition boundaries.
// Every seed checks Node's actual namespace (or abrupt outcome), then changes a generated
// initializer without changing the layout. The model must catch each syntactically valid mutant.
for (let seed = 0; seed < 16; seed++) {
  const length = 3 + (seed % 5);
  const cut = seed % 4 === 0 ? 2 : 1 + (seed % (length - 1));
  const name = (i) => "v" + seed + "_" + i;
  const decls = [];
  for (let i = 0; i < length; i++) {
    const earlier = (seed + i * 3) % Math.max(i, 1);
    const operand = i === 0 ? (seed % 4 === 0 ? name(1) : ["false", "-0", "null", '"abc"'][seed % 4]) : name(earlier);
    const kind = i === 1 && seed % 4 === 0 ? "let" : ["var", "const", "let"][(seed + i) % 3];
    decls.push(
      (i === length - 1 ? "export " : "") + kind + " " + name(i) + " = " + "!".repeat((seed + i) % 4) + operand + ";"
    );
  }
  const code = decls.join("\n");
  const selectors = decls.map((_, i) => "#" + i);
  const generated = path.join(root, "seed-" + seed);
  prepare(code, { [left]: selectors.slice(0, cut), [entry]: selectors.slice(cut) }, [left]);
  assert.equal(check(generated, all, "certified").guarantee, all, code);
  assert.deepEqual(
    await observe(pathToFileURL(path.join(generated, entry))),
    await observe("data:text/javascript," + encodeURIComponent(code)),
    code
  );
  const file = path.join(generated, seed % 2 ? entry : left);
  const contents = fs.readFileSync(file, "utf8");
  const changed = contents.replace(/ = [^;]+/, " = 12345");
  assert.notEqual(changed, contents);
  fs.writeFileSync(file, changed);
  check(generated, all, "rejected", "--check-output");
}
// This cyclic placement needs the entry to expose a previously private binding to its peer.
// Even an order-preserving CoreJS plan with all nine structural checks cannot certify that change.
const leakingSource = "const first = later; var later = 1; export const publicValue = first;";
prepare(leakingSource, { [left]: ["#0"], [entry]: ["#1", "#2"] }, [left]);
const leakage = path.join(root, "leakage");
const structuralOutput = path.join(root, "structural-leakage");
const legacy = spawnSync(process.env.LOWERSPLIT, [input, placement, structuralOutput], { encoding: "utf8" });
assert.equal(legacy.status, 0, legacy.stdout + legacy.stderr);
assert.notDeepEqual(
  await observe(pathToFileURL(path.join(structuralOutput, entry))),
  await observe("data:text/javascript," + encodeURIComponent(leakingSource))
);
assert.match(check(leakage, all, "rejected").reason, /entry public exports/);
assert(!fs.existsSync(leakage));

const reordered = path.join(root, "reordered");
prepare(source, placement3, [right, left]);
assert.match(check(reordered, all, "rejected").reason, /normal-completion/);
assert(!fs.existsSync(reordered));
assert.equal(check(reordered, normal, "certified").guarantee, normal);
assert.deepEqual(await observe(pathToFileURL(path.join(reordered, entry))), [["result", 1]]);
prepare("const opaque = [];", { [entry]: ["#0"] }, []);
const unsupported = path.join(root, "unsupported");
check(unsupported, normal, "rejected");
check(unsupported, normal, "rejected", "--force");
assert(!fs.existsSync(unsupported));

// A valid module AST under another extension has no portable ESM loading contract.
prepare("const local = 1;", { [entry]: [], "other.cjs": ["#0"] }, ["other.cjs"]);
assert.match(check(unsupported, all, "rejected").reason, /\.mjs/);
assert(!fs.existsSync(unsupported));
const escaping = "../escaped.mjs";
prepare("const local = 1;", { [entry]: [], [escaping]: ["#0"] }, [escaping]);
check(unsupported, all, "rejected");
assert(!fs.existsSync(path.join(root, "escaped.mjs")));
