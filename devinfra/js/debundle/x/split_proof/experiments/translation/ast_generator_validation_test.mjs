// Validate the generator's scope metadata using an independently parsed program.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { parse } from "acorn";
import { analyze } from "eslint-scope";
import { generateAstBundle } from "./ast_generator.mjs";

const kinds = new Set();
const programs = new Set();
const heritageShapes = new Set();
const memberKinds = new Set();
const chainDepths = new Set();
let classPrograms = 0;
let functions = 0;
let blocks = 0;
let shadows = 0;
let maxDepth = 0;

const unaryOperands = new Map();
const eagerUnary = new Set();
function walk(value, depth = 0) {
  if (value === null || typeof value !== "object") return;
  if (Array.isArray(value)) {
    for (const child of value) walk(child, depth);
    return;
  }
  if (typeof value.type === "string") {
    kinds.add(value.type);
    if (value.type === "UnaryExpression") {
      if (!unaryOperands.has(value.operator)) unaryOperands.set(value.operator, new Set());
      unaryOperands.get(value.operator).add(value.argument.type);
    }
    maxDepth = Math.max(maxDepth, depth);
    depth++;
  }
  for (const child of Object.values(value)) if (typeof child === "object") walk(child, depth);
}

for (const seed of [1, 2026, 2027, 1337]) {
  for (let round = 0; round < 32; round++) {
    const bundle = generateAstBundle(seed, round);
    assert.equal(bundle.source, generateAstBundle(seed, round).source, "generation must be deterministic");
    programs.add(bundle.source);
    const ast = parse(bundle.source, { ecmaVersion: "latest", sourceType: "module", ranges: true });
    assert.equal(ast.body.length, bundle.units.length, `seed ${seed} round ${round}: unit count`);
    walk(ast);
    const manager = analyze(ast, { ecmaVersion: 2022, sourceType: "module" });
    const moduleScope = manager.scopes.find((s) => s.type === "module");
    assert(moduleScope, "module scope missing");
    for (const scope of manager.scopes) {
      if (scope.type === "function") functions++;
      if (scope.type === "block") blocks++;
      for (const variable of scope.variables) {
        for (let outer = scope.upper; outer; outer = outer.upper) {
          if (outer.set.has(variable.name)) {
            shadows++;
            break;
          }
        }
      }
    }
    const references = manager.scopes.flatMap((s) => s.references).filter((r) => r.resolved?.scope === moduleScope);
    for (const [i, node] of ast.body.entries()) {
      const contains = (n) => node.start <= n.start && n.end <= node.end;
      const defs = moduleScope.variables.filter((v) => v.defs.some((d) => contains(d.name))).map((v) => v.name);
      // eslint-scope reports the declaration's initialization as a write
      // reference; the generator's uses describe references in expressions.
      const uses = references
        .filter((r) => contains(r.identifier) && !(r.init && !r.isRead()))
        .map((r) => r.identifier.name);
      assert.deepEqual(
        [...new Set(bundle.units[i].defs)].sort(),
        [...new Set(defs)].sort(),
        `defs at ${seed}/${round}/${i}`
      );
      assert.deepEqual(
        [...new Set(bundle.units[i].uses)].sort(),
        [...new Set(uses)].sort(),
        `uses at ${seed}/${round}/${i}`
      );
    }
    const result = spawnSync(process.execPath, ["--input-type=module", "--eval", bundle.source], {
      encoding: "utf8",
      timeout: 3000,
    });
    assert.ifError(result.error);
    assert.equal(result.status, 0, `seed ${seed} round ${round} did not complete: ${result.stderr}`);
    // A direct top-level initializer runs whenever the module completes.
    for (const st of ast.body)
      for (const { init } of st.declarations ?? []) if (init?.type === "UnaryExpression") eagerUnary.add(init.operator);
    const classes = ast.body
      .flatMap((st) => st.declarations ?? [])
      .map((d) => d.init)
      .filter((e) => e?.type === "ClassExpression");
    if (classes.length) {
      classPrograms++;
      chainDepths.add(classes.length);
      assert.equal(classes[0].superClass, null);
      assert(classes.length >= 2 && classes.slice(1).every((cls) => cls.superClass?.type === "Identifier"));
      const methods = classes.flatMap((cls) => cls.body.body);
      const observed = result.stdout
        .split("\n")
        .filter((line) => line.startsWith("class "))
        .map((line) => line.split(" ")[1]);
      assert.deepEqual(
        observed,
        methods.map((m) => m.key.name),
        "every generated member must run through the leaf"
      );
      const shape = (m) => `${m.static ? "static" : "instance"} ${m.kind}`;
      methods.forEach((m) => memberKinds.add(shape(m)));
      heritageShapes.add(classes.map((cls) => cls.body.body.map(shape).join(",")).join("/"));
    }
  }
}

for (const type of [
  "FunctionDeclaration",
  "ArrowFunctionExpression",
  "BlockStatement",
  "BinaryExpression",
  "ConditionalExpression",
  "UpdateExpression",
  "AssignmentExpression",
  "CallExpression",
])
  assert(kinds.has(type), `sample did not exercise ${type}`);
assert.deepEqual([...eagerUnary].sort(), ["!", "-"]);
for (const op of ["!", "-"]) {
  const operands = unaryOperands.get(op);
  for (const kind of ["Literal", "Identifier", "BinaryExpression"])
    assert(operands?.has(kind), `missing generated ${op} on ${kind}`);
}
assert.equal(classPrograms, 32, "each seed must execute a class program in one quarter of rounds");
assert.deepEqual([...chainDepths].sort(), [2, 3, 4]);
assert(heritageShapes.size >= 24, "class programs must vary in structure across seeds and rounds");
assert.deepEqual([...memberKinds].sort(), ["instance get", "instance method", "static get", "static method"]);
assert(functions > 0 && blocks > 0 && shadows > 0, "sample lacks function scope, block scope or shadowing");
assert(maxDepth >= 6 && maxDepth <= 40, `unexpected generated AST depth: ${maxDepth}`);
assert(programs.size === 128, `too few distinct programs: ${programs.size}`);
console.log(
  `validated ${programs.size} programs, ${kinds.size} syntax types, depth ${maxDepth}, ${functions} function scopes, ${blocks} blocks, ${shadows} shadows; ${classPrograms} class programs, ${heritageShapes.size} distinct inheritance shapes`
);
