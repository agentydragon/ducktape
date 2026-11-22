// Exercise normal lowersplit output through Acorn and Node after ESTree printing.
import { strict as assert } from "node:assert";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";
import { assertSelectedSourceUnits, parseSourceUnits } from "./ast_assertions.mjs";

const root = process.env.TEST_TMPDIR;
assert(root, "TEST_TMPDIR is required");
const dir = path.join(root, "estree-printer");
mkdirSync(dir, { recursive: true });

const seed = Number(process.argv[2] ?? "2026");
assert(Number.isSafeInteger(seed), `expected an integer seed, got ${seed}`);
function random(seed) {
  let state = seed >>> 0;
  return (limit) => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state % limit;
  };
}

const chooseBodies = [
  "return value ?? (value && ((1 + 2) * 3));",
  "return value ? ((2 + 4) * 3) : (7 ** 2);",
  "return (value || 5) && (3 + 4 * 2);",
  "return value && (1 + (2 * 3));",
];
const patterns = ["/[a-z]+/gi", String.raw`/(?<word>[a-z]+)(?:-(\\d+))?/gim`, "/^alpha(?:-[0-9]+)?/y"];
const bigints = ["0x2An", "0b101010n", "42n", "0o52n"];
const conditions = ["(false || true)", "(true && !false)", "(1 + 2 * 3 > 6)"];
const precedenceArms = ["(1 + 2) * 4", "1 + (2 * 4)", "3 ** 2 ** 2"];
const fallbackArms = ["0", "7 - 2", "8 / (2 + 2)"];
const escapedLiterals = [String.raw`"line\n\u{1F680}"`, String.raw`"tab\ttext"`, String.raw`"escaped quote: \""`];

const choose = random(seed);
const lines = ['"use strict";', '("not a directive");'];
const cases = [];
for (let index = 0; index < 6; index++) {
  const name = `case${index}`;
  const body = chooseBodies[choose(chooseBodies.length)];
  const pattern = patterns[choose(patterns.length)];
  const bigint = bigints[choose(bigints.length)];
  const condition = conditions[choose(conditions.length)];
  const precedence = `${condition} ? ${precedenceArms[choose(precedenceArms.length)]} : ${fallbackArms[choose(fallbackArms.length)]}`;
  const escaped = escapedLiterals[choose(escapedLiterals.length)];
  lines.push(`function choose_${name}(value) {\n  // Source-observable seed ${seed}, case ${index}.\n  ${body}\n}`);
  lines.push(
    `class Box_${name} {\n  #value;\n  constructor(value) { this.#value = value; }\n  read() { return this.#value; }\n}`
  );
  lines.push(`const pattern_${name} = ${pattern};`);
  lines.push(`const amount_${name} = ${bigint} + 1n;`);
  lines.push(`const precedence_${name} = ${precedence};`);
  lines.push(`const escaped_${name} = ${escaped};`);
  lines.push(
    `const methods_${name} = {\n  read(value) {\n    // Object method source is observable as well.\n    return value ?? 17;\n  },\n  get value() {\n    return 42;\n  },\n};`
  );
  cases.push(name);
}
lines.push(
  `console.log(JSON.stringify([${cases
    .map(
      (name) =>
        `choose_${name}(0), new Box_${name}(9).read(), pattern_${name}.flags + ":" + pattern_${name}.exec("alpha-42")?.[0], amount_${name}.toString(), precedence_${name}, escaped_${name}, Function.prototype.toString.call(choose_${name}), Box_${name}.toString(), Function.prototype.toString.call(methods_${name}.read), Function.prototype.toString.call(Object.getOwnPropertyDescriptor(methods_${name}, "value").get)`
    )
    .join(", ")} ]));`
);
const source = lines.join("\n") + "\n";
const ast = path.join(dir, "bundle.json");
const spec = path.join(dir, "spec.json");
const bundle = path.join(dir, "bundle.mjs");
const out = path.join(dir, "out");
writeFileSync(bundle, source);
const sourceProgram = lowerDump(source, acorn, eslintScope);
writeFileSync(ast, JSON.stringify(sourceProgram));

const sourceUnits = parseSourceUnits(source);
const helperOrdinals = sourceUnits.flatMap((node, index) =>
  node.type === "FunctionDeclaration" || node.type === "ClassDeclaration" ? [index] : []
);
const helperSet = new Set(helperOrdinals);
const modules = {
  "helpers.mjs": [...helperSet].map((index) => `#${index}`),
  "entry.mjs": sourceUnits
    .map((_, index) => index)
    .filter((index) => !helperSet.has(index))
    .map((index) => `#${index}`),
};
writeFileSync(spec, JSON.stringify({ entry: "entry.mjs", modules }));

const lowered = spawnSync(process.env.LOWERSPLIT, [ast, spec, out], { encoding: "utf8" });
assert.equal(lowered.status, 0, `${lowered.stdout}\n${lowered.stderr}`);

for (const [file, selectors] of Object.entries(modules)) {
  const emittedText = readFileSync(path.join(out, file), "utf8");
  assertSelectedSourceUnits(sourceUnits, emittedText, selectors, file);
}

const entryProgram = acorn.parse(readFileSync(path.join(out, "entry.mjs"), "utf8"), {
  sourceType: "module",
  ecmaVersion: "latest",
});
assert.equal(
  entryProgram.body[0].directive,
  "use strict",
  "the original directive stays in the prologue ahead of glue imports"
);
const ordinaryString = entryProgram.body.find(
  (node) =>
    node.type === "ExpressionStatement" &&
    node.expression.type === "Literal" &&
    node.expression.value === "not a directive"
);
assert(ordinaryString, "the ordinary string expression survives printing");
assert.equal(ordinaryString.directive, undefined, "printing must not turn a string expression into a directive");
assert(
  entryProgram.body.some((node) => node.type === "ImportDeclaration"),
  "the entry imports its moved callables"
);

const originalOutput = execFileSync(process.execPath, [bundle], { encoding: "utf8" });
const emittedOutput = execFileSync(process.execPath, [path.join(out, "entry.mjs")], { encoding: "utf8" });
assert.equal(
  emittedOutput,
  originalOutput,
  `seed ${seed}: values, regexp/BigInt/precedence behavior, and callable source text must be preserved`
);
