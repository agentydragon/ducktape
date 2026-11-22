// The generator of `gen/`: over many seeds every program parses, runs to the end in Node without an error, prints the same
// twice, prints what it should (a line for every observer and a final one), keeps its names consistent (renaming every
// module-scope and local name changes nothing), and declares before it uses. Each family is tried alone with the ones it
// needs, and all together. Prints what the programs contain; the families must produce the constructs they are for.
//
//   node gen_validity_test.mjs [--samples N] [--seed S]
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { FAMILIES, generateProgram, renderProgram } from "./gen/index.mjs";
import { featuresOf, parseModule } from "./syntax_inventory.mjs";

const option = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i < 0 ? fallback : Number(process.argv[i + 1]);
};
const samples = option("samples", 40);
const firstSeed = option("seed", 1);
const work = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? os.tmpdir(), "gen_validity_"));

const run = (file) => spawnSync(process.execPath, [file], { encoding: "utf8", timeout: 10000 });

/** The forms each family must produce somewhere in the sample: the constructs it exists for. */
const MUST_HAVE = {
  expressions: [
    ...["+", "-", "*", "/", "%", "**", "|", "&", "^", "<<", ">>", ">>>", "==", "!=", "===", "!==", "<", "<=", ">", ">=", "in"].map((o) => `binary:${o}`),
    "logical:&&", "logical:||", "logical:??", "unary:-", "unary:+", "unary:~", "unary:!", "unary:typeof", "unary:void",
    "conditional", "sequence", "member:computed", "optional:member", "template:expressions", "literal:regexp", "literal:regexp-v",
    "literal:numeric-separator", "call:method", "array:spread",
  ], // prettier-ignore
  statements: [
    "if:else", "if:else-if", "for", "for:let", "for:empty-parts", "for-in", "for-of", "while", "do-while", "switch", "switch:default",
    "switch:fallthrough", "switch:lexical", "try:catch", "try:finally", "try:catch-finally", "catch:no-binding", "catch:pattern",
    "label", "label:block", "break:label", "continue:label", "break", "continue", "throw", "block:lexical", "var:in-block",
    "assign:+=", "assign:-=", "assign:*=", "assign:/=", "assign:%=", "assign:**=", "assign:<<=", "assign:>>=", "assign:>>>=",
    "assign:&=", "assign:|=", "assign:^=", "assign:&&=", "assign:||=", "assign:??=", "update:prefix++", "update:postfix++",
    "update:prefix--", "update:postfix--", "update:member", "delete:member", "object:computed", "array:hole", "pattern:array-swap",
    "empty-statement", "debugger",
  ], // prettier-ignore
  functions: [
    "function:declaration", "function:arrow", "function:arrow-expression", "function:named-expression", "function:generator",
    "function:async", "function:async-arrow", "param:default", "param:rest", "arguments", "yield", "yield:delegate", "await",
    "for-await", "call:iife", "object:method", "tagged-template", "tagged-template:expressions",
  ], // prettier-ignore
  classes: [
    "class:declaration", "class:expression", "class:named-expression", "class:extends", "class:extends-expression",
    "class:extends-null", "class:constructor", "method", "method:static", "method:getter", "method:setter",
    "method:static-getter", "method:static-setter", "method:computed", "method:generator", "method:async",
    "method:async-generator", "method:private", "method:private-accessor", "method:static-private", "field",
    "field:no-initializer", "field:static", "field:computed", "field:private", "field:static-private",
    "field:arrow-initializer", "static-block", "new.target", "private-in", "member:private", "member:super", "call:super",
    "new:no-arguments", "new:spread", "new:member",
  ], // prettier-ignore
  patterns: [
    "pattern:default", "pattern:rest-object", "pattern:rest-array", "pattern:nested", "pattern:computed-key", "pattern:hole",
    "pattern:default-array", "pattern:object-assign", "pattern:member-target", "param:destructured", "for-of:destructured",
    "for-of:target", "for-in:target", "for-in:var", "declaration:several", "optional:call", "optional:chain-continues",
    "optional:method-call", "optional:delete", "block:function", "call:spread",
  ], // prettier-ignore
  protocols: [
    "object:getter", "object:setter", "object:async-method", "object:generator-method", "object:computed-method",
    "object:proto", "object:shorthand", "yield:no-argument",
  ], // prettier-ignore
  modules: [
    "import:default",
    "import:named",
    "import:alias",
    "import:namespace",
    "import:side-effect",
    "import()",
    "import.meta",
  ],
  unmodeled: ["literal:bigint"],
};
/** What a family must write besides the forms: calls have no form of their own. */
const MUST_CONTAIN = {
  protocols: [
    /new Proxy\(/,
    /Symbol\.toPrimitive/,
    /Symbol\.iterator/,
    /Object\.defineProperty\(/,
    /Reflect\.\w+\(/,
    /Object\.freeze\(/,
    /Object\.create\(/,
  ],
  modules: [/new EventEmitter\(/, /createHash\(/],
  unmodeled: [
    /new (Uint8Array|Int16Array|Float64Array|Uint32Array|Int8Array)\(/,
    /\.subarray\(/,
    /new DataView\(/,
    /\beval\(/,
    /new Function\(/,
    /queueMicrotask\(/,
    /process\.nextTick\(/,
    /new WeakRef\(/,
    /new WeakMap\(/,
  ],
};
// The families are tried cumulatively, each with the ones before it: the expressions make no unit by themselves. A set must
// produce the forms of the families in it.
const SETS = [
  ["expressions", "statements"],
  ["expressions", "statements", "functions"],
  ["expressions", "statements", "functions", "classes"],
  ["expressions", "statements", "functions", "classes", "patterns"],
  ["expressions", "statements", "functions", "classes", "patterns", "protocols"],
  ["expressions", "statements", "functions", "classes", "patterns", "protocols", "modules", "unmodeled"],
];
assert.deepEqual([...new Set(SETS.flat())].sort(), Object.keys(FAMILIES).sort(), "a family is not tried");

function check(seed, families) {
  const program = generateProgram(seed, { families });
  const source = renderProgram(program);
  assert.equal(
    source,
    renderProgram(generateProgram(seed, { families })),
    `seed ${seed}: generation is not deterministic`
  );
  const ast = parseModule(source);
  assert(program.units.length >= 10, `seed ${seed}: only ${program.units.length} units`);
  // a name is declared once, before every unit that uses it
  const declared = new Set();
  for (const unit of program.units) {
    for (const name of unit.uses)
      assert(declared.has(name), `seed ${seed}: ${name} is used before it is declared, in ${unit.src.slice(0, 80)}`);
    for (const name of unit.defs) {
      assert(!declared.has(name), `seed ${seed}: ${name} is declared twice`);
      declared.add(name);
    }
    assert(
      // eslint-disable-next-line no-control-regex -- Validate the generator’s reserved name markers.
      !/[\u0001-\u0004]/.test(unit.src.replace(/\u0001[^\u0002]*\u0002|\u0003[^\u0004]*\u0004/g, "")),
      `seed ${seed}: a stray marker`
    );
  }
  const file = path.join(work, `p${seed}.mjs`);
  writeFileSync(file, source);
  const first = run(file);
  assert.equal(
    first.status,
    0,
    `seed ${seed} fails in Node: ${first.stderr.split("\n").slice(0, 6).join("\n")}\n${source.slice(0, 600)}`
  );
  assert.equal(first.stderr, "", `seed ${seed} writes to stderr`);
  assert.equal(run(file).stdout, first.stdout, `seed ${seed} prints something else the second time`);
  assert.match(first.stdout, /^final /m, `seed ${seed} has no final line`);
  // renaming every name the generator marks leaves the output as it is: no name is spelled without its marker
  const renamed = path.join(work, `r${seed}.mjs`);
  writeFileSync(renamed, renderProgram(program, { top: (n) => `r_${n}`, local: (n) => `l_${n}` }));
  const again = run(renamed);
  assert.equal(again.status, 0, `seed ${seed}, renamed: ${again.stderr.split("\n").slice(0, 4).join("\n")}`);
  assert.equal(again.stdout, first.stdout, `seed ${seed}: renaming changes the output`);
  return { units: program.units.length, features: featuresOf(ast), source };
}

const summary = {};
for (const families of SETS) {
  const found = new Set();
  const types = new Set();
  const written = new Set();
  let units = 0;
  const required = (family) => [...(MUST_HAVE[family] ?? []), ...(MUST_CONTAIN[family] ?? [])];
  /** Is a requirement (a form, or a regular expression the source must match) met by the program? */
  const meets = (requirement, forms, source) =>
    typeof requirement === "string" ? forms.has(requirement) : requirement.test(source);
  for (let seed = firstSeed; seed < firstSeed + samples; seed++) {
    const r = check(seed, families);
    units += r.units;
    for (const f of r.features.forms.keys()) found.add(f);
    for (const t of r.features.types.keys()) types.add(t);
    for (const requirement of families.flatMap(required))
      if (meets(requirement, r.features.forms, r.source)) written.add(String(requirement));
  }
  // a requirement that is rare in the programs that were run is looked for in more, which are only generated and parsed
  let missing = families.flatMap(required).filter((q) => !written.has(String(q)));
  const more = firstSeed + samples + 400;
  for (let seed = firstSeed + samples; missing.length > 0 && seed < more; seed++) {
    const source = renderProgram(generateProgram(seed, { families }));
    const forms = featuresOf(parseModule(source)).forms;
    missing = missing.filter((q) => !meets(q, forms, source));
  }
  assert.deepEqual(
    missing,
    [],
    `families ${families}: ${more - firstSeed} programs have no ${missing.map(String).join(", ")}`
  );
  summary[families.join("+")] = { programs: samples, units, nodeTypes: types.size, forms: found.size };
}
console.log("generator validity:", JSON.stringify(summary));
rmSync(work, { recursive: true, force: true });
