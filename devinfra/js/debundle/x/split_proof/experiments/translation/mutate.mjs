// Mutation test of the Lean checker: tamper with one thing in an accepted output, keep the certificate, and check that
// `translate` rejects it.
//
//   node mutate.mjs <bundle.json> <output.json> <cert.json> [--count N] [--seed S]
//
// Mutants: a literal changed, an operator changed, a reference re-pointed at another module-scope name, a local
// reference re-pointed at another local, a statement dropped from a block, two call arguments swapped. A mutant that is
// still accepted is reported (it may be a no-op, for instance a swap of equal arguments).
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { bodyUnits } from "./frontend.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const translate = process.env.TRANSLATE ?? path.join(here, "../../lean/.lake/build/bin/translate");
const [bundlePath, outputPath, certPath, ...rest] = process.argv.slice(2);
const option = (name, fallback) =>
  rest.includes(`--${name}`) ? Number(rest[rest.indexOf(`--${name}`) + 1]) : fallback;
const count = option("count", 12);
let seed = option("seed", 1);
const random = (n) => {
  seed = (seed * 1103515245 + 12345) % 2147483648;
  return seed % n;
};

const output = JSON.parse(readFileSync(outputPath, "utf8"));
const candidates = {
  literal: [],
  operator: [],
  topReference: [],
  localReference: [],
  dropStatement: [],
  swapArguments: [],
};
const topNamesOf = output.modules.map(() => new Set());
output.modules.forEach((module, m) => {
  for (const unit of bodyUnits(module.program)) {
    const binders = new Set();
    const walk = (node, parent, key) => {
      if (Array.isArray(node)) {
        node.forEach((child, i) => walk(child, node, i));
        if (node.length >= 2 && key === "body" && parent?.type === "BlockStatement")
          candidates.dropStatement.push({ array: node });
        return;
      }
      if (node === null || typeof node !== "object") return;
      if (node.type === "Literal" && /^[sn]:/.test(node.lit)) candidates.literal.push({ node });
      else if (node.type === "BinaryExpression" && ["+", "-", "*", "<", ">", "==="].includes(node.operator))
        candidates.operator.push({ node });
      else if (
        node.type === "CallExpression" &&
        node.arguments.length >= 2 &&
        JSON.stringify(node.arguments[0]) !== JSON.stringify(node.arguments[1])
      )
        candidates.swapArguments.push({ node });
      if (node.type === "Identifier" && node.k === "T") {
        topNamesOf[m].add(node.name);
        if (node.d !== 1) candidates.topReference.push({ node, m });
      }
      if (node.type === "Identifier" && node.k === "L") {
        binders.add(node.b);
        candidates.localReference.push({ node, binders });
      }
      for (const [k, v] of Object.entries(node)) walk(v, node, k);
    };
    walk(unit, null, null);
  }
});

const work = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? tmpdir(), "mutate_"));
const kinds = Object.keys(candidates);
let survived = 0;
for (let n = 0; n < count; n++) {
  const kind = kinds[n % kinds.length];
  const pool = candidates[kind];
  if (pool.length === 0) continue;
  const pick = pool[random(pool.length)];
  let undo;
  let what;
  if (kind === "literal") {
    const old = pick.node.lit;
    pick.node.lit = old.startsWith("s:") ? `${old}!` : `n:${Number(old.slice(2)) + 1}`;
    undo = () => (pick.node.lit = old);
    what = `literal ${old}`;
  } else if (kind === "operator") {
    const old = pick.node.operator;
    pick.node.operator = old === "+" ? "-" : "+";
    undo = () => (pick.node.operator = old);
    what = `operator ${old}`;
  } else if (kind === "topReference") {
    const old = pick.node.name;
    const others = [...topNamesOf[pick.m]].filter((x) => x !== old);
    if (others.length === 0) continue;
    pick.node.name = others[random(others.length)];
    undo = () => (pick.node.name = old);
    what = `reference ${old} -> ${pick.node.name}`;
  } else if (kind === "localReference") {
    const old = pick.node.b;
    const others = [...pick.binders].filter((x) => x !== old);
    if (others.length === 0) continue;
    pick.node.b = others[random(others.length)];
    undo = () => (pick.node.b = old);
    what = `local binder ${old} -> ${pick.node.b}`;
  } else if (kind === "dropStatement") {
    const index = random(pick.array.length);
    const removed = pick.array.splice(index, 1);
    undo = () => pick.array.splice(index, 0, ...removed);
    what = "statement dropped";
  } else {
    const args = pick.node.arguments;
    [args[0], args[1]] = [args[1], args[0]];
    undo = () => ([args[0], args[1]] = [args[1], args[0]]);
    what = "arguments swapped";
  }
  const file = path.join(work, "mutant.json");
  writeFileSync(file, JSON.stringify(output));
  undo();
  const result = spawnSync(translate, [bundlePath, file, certPath], { encoding: "utf8", maxBuffer: 1 << 28 });
  if (result.error) throw result.error;
  const rejection = result.stdout.split("\n").find((l) => /^(TRANSLATION REJECT|PLAN NOT SOUND):/.test(l));
  const verdict = rejection ?? result.stdout.split("\n").find((l) => /^TRANSLATION/.test(l)) ?? result.stderr;
  const rejected = result.status === 1 && rejection !== undefined;
  if (!rejected) survived++;
  console.log(`${rejected ? "rejected" : "SURVIVED"}  ${kind}: ${what}  ->  ${verdict.slice(0, 120)}`);
}
rmSync(work, { recursive: true, force: true });
if (survived) throw new Error(`${survived} mutants were still accepted`);
