// Broad grammar-generated ASTs supplement the targeted, scope-aware corpus.
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { cpSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { dump as dumpSource } from "./parser.mjs";
import { shiftCandidate } from "./shift_candidate.mjs";
import { walk } from "./syntax_inventory.mjs";
import { writeDeployment } from "./testing/deployment_manifest.mjs";
import { topLevelStatements } from "./scope_metadata.mjs";

// Named source-syntax families from the input requirements. Counts describe
// syntactic presence, not execution of every branch or semantic equivalence.
const features = [
  "VariableDeclaration:var",
  "VariableDeclaration:let",
  "VariableDeclaration:const",
  "VariableDeclaration:using",
  "VariableDeclaration:await using",
  "ArrayPattern",
  "ObjectPattern",
  "AssignmentPattern",
  "RestElement",
  "SpreadElement",
  "FunctionDeclaration",
  "FunctionExpression",
  "ArrowFunctionExpression",
  "function:async",
  "function:generator",
  "function:async-generator",
  "ClassDeclaration",
  "ClassExpression",
  "class:extends",
  "PrivateIdentifier",
  "PropertyDefinition",
  "MethodDefinition",
  "StaticBlock",
  "class:private-field",
  "class:private-method",
  "property:computed",
  "property:get",
  "property:set",
  "IfStatement",
  "SwitchStatement",
  "ForStatement",
  "ForInStatement",
  "ForOfStatement",
  "for:await",
  "WhileStatement",
  "DoWhileStatement",
  "TryStatement",
  "CatchClause",
  "ThrowStatement",
  "LabeledStatement",
  "BreakStatement",
  "ContinueStatement",
  "AwaitExpression",
  "YieldExpression",
  "TemplateLiteral",
  "TaggedTemplateExpression",
  "literal:regexp",
  "literal:bigint",
  "ChainExpression",
  "ImportDeclaration",
  "ImportSpecifier",
  "ImportDefaultSpecifier",
  "ImportNamespaceSpecifier",
  "ImportExpression",
  "ExportNamedDeclaration",
  "ExportDefaultDeclaration",
  "ExportAllDeclaration",
  "meta:import.meta",
  "meta:new.target",
  "CallExpression",
  "NewExpression",
  "ThisExpression",
  "Super",
  "SequenceExpression",
  "ConditionalExpression",
  ...[
    "+",
    "-",
    "*",
    "/",
    "%",
    "**",
    "==",
    "===",
    "!=",
    "!==",
    "<",
    ">",
    "<=",
    ">=",
    "in",
    "instanceof",
    "|",
    "&",
    "^",
    "<<",
    ">>",
    ">>>",
  ].map((op) => `BinaryExpression:${op}`),
  ...["&&", "||", "??"].map((op) => `LogicalExpression:${op}`),
  ...["=", "+=", "-=", "*=", "/=", "%=", "**=", "|=", "&=", "^=", "<<=", ">>=", ">>>=", "&&=", "||=", "??="].map(
    (op) => `AssignmentExpression:${op}`
  ),
  ...["!", "~", "+", "-", "typeof", "void", "delete"].map((op) => `UnaryExpression:${op}`),
  "UpdateExpression:++",
  "UpdateExpression:--",
];
function syntaxFeatures(root, found = new Set(), types) {
  walk(root, (node) => {
    types?.add(node.type);
    found.add(node.type);
    if (node.type === "VariableDeclaration") found.add(`${node.type}:${node.kind}`);
    if (node.operator) found.add(`${node.type}:${node.operator}`);
    if (node.type.includes("Function") || node.type === "ArrowFunctionExpression") {
      if (node.async) found.add("function:async");
      if (node.generator) found.add("function:generator");
      if (node.async && node.generator) found.add("function:async-generator");
    }
    if (node.type.startsWith("Class") && node.superClass) found.add("class:extends");
    if (
      (node.type === "Property" || node.type === "MethodDefinition" || node.type === "PropertyDefinition") &&
      node.computed
    )
      found.add("property:computed");
    if ((node.type === "Property" || node.type === "MethodDefinition") && (node.kind === "get" || node.kind === "set"))
      found.add(`property:${node.kind}`);
    if (node.type === "PropertyDefinition" && node.key.type === "PrivateIdentifier") found.add("class:private-field");
    if (node.type === "MethodDefinition" && node.key.type === "PrivateIdentifier") found.add("class:private-method");
    if (node.type === "ForOfStatement" && node.await) found.add("for:await");
    if (node.type === "Literal" && node.regex) found.add("literal:regexp");
    if (node.type === "Literal" && node.bigint !== undefined) found.add("literal:bigint");
    if (node.type === "MetaProperty") found.add(`meta:${node.meta.name}.${node.property.name}`);
  });
  return found;
}
function emptyCoverage() {
  return Object.fromEntries(
    features.map((feature) => [
      feature,
      {
        parsed: 0,
        moduleSyntax: 0,
        small: 0,
        baselineTimeout: 0,
        unstable: 0,
        loweringRejected: 0,
        emitterRejected: 0,
        acceptedThrowing: 0,
        acceptedSame: 0,
        acceptedSameReordered: 0,
        acceptedSameInSide: 0,
      },
    ])
  );
}

const root = process.env.TEST_TMPDIR;
assert(root);
const worker = fileURLToPath(new URL("./shift_fuzz_worker.mjs", import.meta.url));
const option = (name, fallback) => {
  const i = process.argv.lastIndexOf(`--${name}`);
  return i < 0 ? fallback : Number(process.argv[i + 1]);
};
const count = option("count", 128);
const startSeed = option("seed", 1);
const scriptGrammar = process.argv.includes("--script-grammar");
const modern = process.argv.includes("--modern-syntax");
assert(Number.isInteger(count) && count > 0 && Number.isInteger(startSeed) && startSeed >= 0);
function run(rootDir, entry, marker = null) {
  const child = spawnSync(
    process.execPath,
    ["--max-old-space-size=128", "--experimental-vm-modules", worker, rootDir, entry, ...(marker ? [marker] : [])],
    {
      encoding: "utf8",
      timeout: 2000,
      maxBuffer: 1048576,
      env: {
        PATH: process.env.PATH,
        ...Object.fromEntries(Object.entries(process.env).filter(([key]) => key.startsWith("JS_BINARY__"))),
      },
    }
  );
  if (child.error?.code === "ETIMEDOUT") return { status: "timeout" };
  if (child.error || child.status !== 0) throw new Error(`module worker failed: ${child.error ?? child.stderr}`);
  try {
    return JSON.parse(child.stdout);
  } catch (error) {
    throw new Error(`module worker produced invalid JSON: ${error}; stderr=${child.stderr}`);
  }
}
const stats = {
  attempted: 0,
  parseRejected: 0,
  moduleSyntax: 0,
  small: 0,
  baselineTimeout: 0,
  baselineThrows: 0,
  unstable: 0,
  loweringRejected: 0,
  emitterRejected: 0,
  acceptedThrowing: 0,
  accepted: 0,
  acceptedOk: 0,
  reordered: 0,
  reorderedOk: 0,
  differing: 0,
  modernComposed: 0,
  instrumentationRejected: 0,
  modernExecuted: { nullish: 0, optional: 0, field: 0 },
  modernReorderedExecuted: { nullish: 0, optional: 0, field: 0 },
};
const coverage = emptyCoverage();
const syntax = new Set(),
  acceptedSyntax = new Set();
for (let seed = startSeed; seed < startSeed + count; seed++) {
  stats.attempted++;
  const candidate = shiftCandidate(seed, { script: scriptGrammar, modern });
  const { ast: parsed, random, shift, marker, sites, skip } = candidate;
  if (sites) stats.modernComposed++;
  if (!parsed) {
    stats[skip]++;
    continue;
  }
  const present = syntaxFeatures(parsed, new Set(), syntax);
  const record = (outcome, selected = present) => {
    for (const feature of features) if (selected.has(feature)) coverage[feature][outcome]++;
  };
  record("parsed");
  if (skip) {
    stats[skip]++;
    record(skip);
    continue;
  }
  // Observe initialized top-level values without accessing arbitrary object
  // properties. The observer is kept last in the entry module.
  const names = parsed.body.flatMap((s) =>
    s.type === "VariableDeclaration"
      ? s.declarations.filter((d) => d.id.type === "Identifier").map((d) => d.id.name)
      : ["FunctionDeclaration", "ClassDeclaration"].includes(s.type) && s.id
        ? [s.id.name]
        : []
  );
  const unique = [...new Set(names)].slice(0, 8);
  const source =
    candidate.source +
    `\nconsole.log("__bindings", ${unique.map((n) => `(typeof ${n} === "object" || typeof ${n} === "function" ? typeof ${n} : ${n})`).join(", ")});\n`;
  const dir = path.join(root, `seed-${seed}`);
  const out = path.join(dir, "out");
  mkdirSync(out, { recursive: true });
  writeFileSync(path.join(dir, "original.mjs"), source);
  const baseline = run(dir, "original.mjs", marker);
  if (!baseline || baseline.status === "timeout") {
    stats.baselineTimeout++;
    record("baselineTimeout");
    continue;
  }
  if (baseline.status !== "ok") stats.baselineThrows++;
  const repeat = run(dir, "original.mjs", marker);
  if (JSON.stringify(baseline) !== JSON.stringify(repeat)) {
    stats.unstable++;
    record("unstable");
    continue;
  }
  let dump;
  try {
    dump = dumpSource(source);
  } catch {
    stats.loweringRejected++;
    record("loweringRejected");
    continue;
  }
  // Source units exclude the binding observer appended to the entry module.
  const units = topLevelStatements(parsed);
  const side = [];
  for (let i = 0; i < units.length; i++) if (random() < 0.5) side.push(i);
  if (!side.length) side.push(0);
  const spec = {
    entry: "entry.mjs",
    modules: {
      "entry.mjs": Array.from({ length: units.length + 1 }, (_, i) => i)
        .filter((i) => !side.includes(i))
        .map((i) => `#${i}`),
      "side.mjs": side.map((i) => `#${i}`),
    },
  };
  writeFileSync(path.join(dir, "ast.json"), JSON.stringify(dump));
  writeFileSync(path.join(dir, "spec.json"), JSON.stringify(spec));
  const emitter = spawnSync(
    process.env.SPLIT_PREVIEW,
    [writeDeployment(path.join(dir, "ast.json"), path.join(dir, "spec.json"), out), out],
    {
      encoding: "utf8",
      // A cold Lean process loads the compiler before the splitter can read the case.
      timeout: 60_000,
    }
  );
  if (emitter.error) {
    if (process.env.TEST_UNDECLARED_OUTPUTS_DIR)
      cpSync(dir, path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, `emitter-error-${seed}`), { recursive: true });
    throw new Error(
      `seed ${seed}: emitter failed: ${emitter.error}; source=${JSON.stringify(source)}; stdout=${emitter.stdout}; stderr=${emitter.stderr}`
    );
  }
  if (emitter.status !== 0) {
    if (![1, 3].includes(emitter.status))
      throw new Error(`seed ${seed}: emitter exited ${emitter.status}: ${emitter.stderr}`);
    if (emitter.status === 1 && !emitter.stderr.includes("rejected; nothing written"))
      throw new Error(`seed ${seed}: unexpected emitter failure: ${emitter.stderr}`);
    stats.emitterRejected++;
    record("emitterRejected");
    continue;
  }
  stats.accepted++;
  if (baseline.status === "ok") stats.acceptedOk++;
  walk(parsed, (node) => acceptedSyntax.add(node.type));
  const executed =
    modern && baseline.status === "ok"
      ? Object.keys(stats.modernExecuted).filter((kind) => baseline.modern.includes(kind))
      : [];
  for (const kind of executed) stats.modernExecuted[kind]++;
  const match = emitter.stdout.match(/statement order\s+\[([^\]]*)\]/);
  let reordered = false;
  if (match) {
    const order = match[1].split(",").map((x) => Number(x.trim()));
    reordered = order.some((item, i) => item !== i);
    if (reordered) {
      stats.reordered++;
      if (baseline.status === "ok") stats.reorderedOk++;
      for (const kind of executed) stats.modernReorderedExecuted[kind]++;
    }
  }
  // Premise P4: the checker's claim is for sources that complete. After a reorder, a source that throws may throw
  // something else, or throw first.
  if (baseline.status !== "ok") {
    stats.acceptedThrowing++;
    record("acceptedThrowing");
    continue;
  }
  const emitted = run(out, "entry.mjs", marker);
  if (!emitted) throw new Error(`seed ${seed}: emitted tree did not complete; artifacts at ${dir}`);
  if (JSON.stringify(baseline) !== JSON.stringify(emitted)) {
    stats.differing++;
    if (process.env.TEST_UNDECLARED_OUTPUTS_DIR) {
      writeFileSync(path.join(dir, "shift-ast.json"), JSON.stringify(shift));
      writeFileSync(path.join(dir, "observed.json"), JSON.stringify({ baseline, emitted, seed }));
      cpSync(dir, path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, `wrong-accept-${seed}`), { recursive: true });
    }
    throw new Error(
      `seed ${seed}: accepted output differs: ${JSON.stringify({ baseline, emitted })}; artifacts at ${dir}`
    );
  }
  record("acceptedSame");
  if (reordered) record("acceptedSameReordered");
  const sideFeatures = new Set();
  for (const index of side) syntaxFeatures(units[index].node, sideFeatures);
  record("acceptedSameInSide", sideFeatures);
}
for (const row of Object.values(coverage)) {
  assert.equal(
    row.parsed,
    row.moduleSyntax +
      row.small +
      row.baselineTimeout +
      row.unstable +
      row.loweringRejected +
      row.emitterRejected +
      row.acceptedThrowing +
      row.acceptedSame
  );
  assert(row.acceptedSameInSide <= row.acceptedSame);
  assert(row.acceptedSameReordered <= row.acceptedSame);
  // The report retains its Ok fields. Under P4 only completing baselines reach the comparison.
  row.acceptedSameOk = row.acceptedSame;
  row.acceptedSameOkInSide = row.acceptedSameInSide;
  row.acceptedSameOkReordered = row.acceptedSameReordered;
}
if (process.env.TEST_UNDECLARED_OUTPUTS_DIR)
  writeFileSync(
    path.join(process.env.TEST_UNDECLARED_OUTPUTS_DIR, `syntax-coverage-${startSeed}.json`),
    JSON.stringify({ seed: startSeed, count, coverage }, null, 2)
  );
console.log("syntax coverage:", JSON.stringify(coverage));
console.log(
  JSON.stringify({
    seed: startSeed,
    grammar: scriptGrammar ? "script" : "module",
    modern,
    ...stats,
    syntax: [...syntax].sort(),
    acceptedSyntax: [...acceptedSyntax].sort(),
  })
);
assert(stats.accepted > 0, "no executable generated split was accepted");
assert(stats.reordered > 0, "no accepted generated split reordered statements");
if (scriptGrammar) assert(stats.reorderedOk > 0, "no successful generated split reordered statements");

if (modern)
  for (const [kind, n] of Object.entries(stats.modernReorderedExecuted))
    assert(n > 0, `no successful reordered split executed ${kind}`);
