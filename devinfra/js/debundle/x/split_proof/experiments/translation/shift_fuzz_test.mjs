// Broad grammar-generated ASTs supplement the targeted, scope-aware corpus.
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { cpSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import shiftFuzzer from "shift-fuzzer";
import codegen from "shift-codegen";
import { composeModernExpressions } from "./modern_syntax.mjs";
import { lowerDump } from "../lowering/dump_core.mjs";

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
function syntaxFeatures(node, found = new Set()) {
  if (!node || typeof node !== "object") return found;
  if (Array.isArray(node)) {
    for (const item of node) syntaxFeatures(item, found);
    return found;
  }
  if (node.type) {
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
  }
  for (const value of Object.values(node)) if (value && typeof value === "object") syntaxFeatures(value, found);
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
        acceptedSame: 0,
        acceptedSameOk: 0,
        acceptedSameReordered: 0,
        acceptedSameOkReordered: 0,
        acceptedSameInSide: 0,
        acceptedSameOkInSide: 0,
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
function rng(seed) {
  let n = seed >>> 0;
  return () => {
    n = (n + 0x6d2b79f5) >>> 0;
    let t = Math.imul(n ^ (n >>> 15), n | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
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
function collect(node, target) {
  if (!node || typeof node !== "object") return;
  if (Array.isArray(node)) return node.forEach((child) => collect(child, target));
  if (typeof node.type === "string") target.add(node.type);
  for (const value of Object.values(node)) if (value && typeof value === "object") collect(value, target);
}
for (let seed = startSeed; seed < startSeed + count; seed++) {
  stats.attempted++;
  const random = rng(seed);
  const shift = (scriptGrammar ? shiftFuzzer.fuzzScript : shiftFuzzer.fuzzModule)(
    new shiftFuzzer.FuzzerState({ maxDepth: 4, rng: random })
  );
  let source = codegen.default ? codegen.default(shift) : codegen(shift);
  let parsed;
  try {
    parsed = acorn.parse(source, { ecmaVersion: "latest", sourceType: "module" });
  } catch {
    stats.parseRejected++;
    continue;
  }
  let marker = null;
  if (modern) {
    marker = `__shift_modern_marker_${seed}`;
    // Restrict generated code that could discover the non-enumerable marker
    // through the VM global or dynamic code. The marker is also excluded from
    // the original source, so it cannot be shadowed or referenced directly.
    const forbidden = new Set([
      "globalThis",
      "global",
      "self",
      "window",
      "eval",
      "Function",
      "Reflect",
      "Proxy",
      marker,
    ]);
    let unsafe = false;
    (function scan(node) {
      if (!node || typeof node !== "object") return;
      if (Array.isArray(node)) return node.forEach(scan);
      if (
        node.type === "ThisExpression" ||
        node.type === "ImportExpression" ||
        (node.type === "Identifier" && forbidden.has(node.name))
      )
        unsafe = true;
      for (const value of Object.values(node)) if (value && typeof value === "object") scan(value);
    })(parsed);
    if (unsafe) {
      stats.instrumentationRejected++;
      continue;
    }
    const composed = composeModernExpressions(source, parsed, rng(seed ^ 0x8b11c5), marker);
    if (composed.sites) stats.modernComposed++;
    source = composed.source;
    try {
      parsed = acorn.parse(source, { ecmaVersion: "latest", sourceType: "module" });
    } catch {
      stats.parseRejected++;
      continue;
    }
  }
  collect(parsed, syntax);
  const present = syntaxFeatures(parsed);
  const record = (outcome, selected = present) => {
    for (const feature of features) if (selected.has(feature)) coverage[feature][outcome]++;
  };
  record("parsed");
  if (parsed.body.some((s) => /^(Import|Export)/.test(s.type))) {
    stats.moduleSyntax++;
    record("moduleSyntax");
    continue;
  }
  if (parsed.body.length < 2 || parsed.body.length > 30) {
    stats.small++;
    record("small");
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
  source += `\nconsole.log("__bindings", ${unique.map((n) => `(typeof ${n} === "object" || typeof ${n} === "function" ? typeof ${n} : ${n})`).join(", ")});\n`;
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
    dump = lowerDump(source, acorn, eslintScope);
  } catch {
    stats.loweringRejected++;
    record("loweringRejected");
    continue;
  }
  const total = dump.statements.length;
  const side = [];
  for (let i = 0; i < total - 1; i++) if (random() < 0.5) side.push(i);
  if (!side.length) side.push(0);
  const spec = {
    entry: "entry.mjs",
    modules: {
      "entry.mjs": Array.from({ length: total }, (_, i) => i)
        .filter((i) => !side.includes(i))
        .map((i) => `#${i}`),
      "side.mjs": side.map((i) => `#${i}`),
    },
  };
  writeFileSync(path.join(dir, "ast.json"), JSON.stringify(dump));
  writeFileSync(path.join(dir, "spec.json"), JSON.stringify(spec));
  const emitter = spawnSync(process.env.LOWERSPLIT, [path.join(dir, "ast.json"), path.join(dir, "spec.json"), out], {
    encoding: "utf8",
    timeout: 20000,
  });
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
  collect(parsed, acceptedSyntax);
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
  for (const index of side) syntaxFeatures(parsed.body[index], sideFeatures);
  record("acceptedSameInSide", sideFeatures);
  if (baseline.status === "ok") {
    record("acceptedSameOk");
    if (reordered) record("acceptedSameOkReordered");
    record("acceptedSameOkInSide", sideFeatures);
  }
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
      row.acceptedSame
  );
  assert(row.acceptedSameOkInSide <= row.acceptedSameInSide && row.acceptedSameInSide <= row.acceptedSame);
  assert(row.acceptedSameOkReordered <= row.acceptedSameReordered && row.acceptedSameReordered <= row.acceptedSame);
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
