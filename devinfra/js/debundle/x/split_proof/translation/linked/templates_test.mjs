import assert from "node:assert/strict";
import path from "node:path";
import {
  root,
  caseShard,
  id,
  literal,
  call,
  returns,
  throws,
  block,
  tryStatement,
  catchClause,
  arrow,
  variables,
  variable,
  exportNamed,
  program,
  observe,
  lower,
  prepare,
  workerSplit,
  assertRejectedOrOrderKeptSlice,
  assertWorkerResult,
  observedProgram,
} from "./harness.mjs";

const runCase = caseShard();

// Vary substitution count and value type. The helper's parameter shadows the
// outer binding, and the escaped newline must come from the cooked segment.
for (const [value, expressions, expected] of [
  [0, [], "head\n"],
  [false, [id("input")], "head\nfalse/"],
  ["middle", [id("prefix"), id("input"), literal(null)], "head\ncaptured/middle/null/"],
]) {
  if (!runCase(`generated-template-${value}`)) continue;
  const quasis = Array.from({ length: expressions.length + 1 }, (_, i) => ({
    type: "TemplateElement",
    value: i === 0 ? { raw: "head\\n", cooked: "head\n" } : { raw: "/", cooked: "/" },
    tail: i === expressions.length,
  }));
  const worker = arrow([id("input")], { type: "TemplateLiteral", expressions, quasis });
  const source = program([
    variables("const", [
      ["input", literal("outer")],
      ["prefix", literal("captured")],
      ["format", worker],
    ]),
    variable("const", "result", call(id("format"), [literal(value)])),
    exportNamed([["result", "result"]]),
  ]);
  const dir = path.join(root, `generated-template-${value}`);
  const files = prepare(dir, {
    source,
    provider: "",
    spec: {
      entry: "entry.mjs",
      modules: { "format.mjs": ["#0", "#1", "#2"], "entry.mjs": ["#3", "#4"] },
      order: ["format.mjs"],
    },
  });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  assert.equal(verdict.status, "certified", log);
  for (const file of [files.sourceFile, path.join(out, "entry.mjs")]) {
    assert.deepEqual(observe(file, "result"), { result: expected });
  }
}

// Both substitutions throw. Only the leftmost thrown value reaches the catch.
for (const values of [
  ["first", "second"],
  ["second", "first"],
]) {
  if (!runCase(`throwing-template-${values[0]}`)) continue;
  const expressions = values.map((value) => call(arrow([], block([throws(literal(value))]))));
  const template = {
    type: "TemplateLiteral",
    expressions,
    quasis: ["", "/", ""].map((text, i) => ({
      type: "TemplateElement",
      value: { raw: text, cooked: text },
      tail: i === expressions.length,
    })),
  };
  const worker = arrow(
    [],
    block([tryStatement(block([returns(template)]), catchClause(id("error"), block([returns(id("error"))])))])
  );
  assertWorkerResult(`throwing-template-${values[0]}`, observedProgram(worker, call(id("worker"))), values[0]);
}

for (const [name, source, reason] of [
  [
    "template-object-coercion",
    "const worker = value => `${value}`; export const result = worker({});",
    /object is converted to a primitive/,
  ],
]) {
  if (!runCase(name)) continue;
  const result = workerSplit(name, source);
  if (assertRejectedOrOrderKeptSlice(result, reason)) {
    assert.deepEqual(observe(path.join(result.out, "entry.mjs"), "result"), observe(result.files.sourceFile, "result"));
  }
}
