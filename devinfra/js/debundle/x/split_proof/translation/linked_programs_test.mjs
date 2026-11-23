import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  root,
  id,
  literal,
  call,
  conditional,
  returns,
  throws,
  block,
  tryStatement,
  catchClause,
  arrow,
  variables,
  variable,
  property,
  exportNamed,
  program,
  observe,
  lower,
  prepare,
} from "./linked_programs_test_support.mjs";

// Move the first declaration to its own module and observe the result exported by the entry.
function workerSplit(name, source) {
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source,
    provider: "",
    spec: { entry: "entry.mjs", modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1"] }, order: ["worker.mjs"] },
  });
  const out = path.join(dir, "out");
  return { files, out, ...lower(files.sourceFile, files.specFile, out) };
}

function assertWorkerResult(name, source, expected) {
  const { files, out, verdict, log } = workerSplit(name, source);
  assert.equal(verdict.status, "certified", `${name}: ${log}`);
  assert.equal(verdict.guarantee, "normal-completion", log);
  for (const file of [files.sourceFile, path.join(out, "entry.mjs")]) {
    assert.deepEqual(observe(file, "result"), { result: expected }, name);
  }
}

function observedProgram(initializer, result) {
  return program([
    variable("const", "worker", initializer),
    {
      type: "ExportNamedDeclaration",
      declaration: variable("const", "result", result),
      specifiers: [],
      source: null,
    },
  ]);
}

for (const [seed, value] of [0, "taken", "", 3].entries()) {
  const selected = literal(seed + 30);
  const skipped = call(arrow([], block([throws(literal(seed))])));
  const choose = conditional(literal(value), value ? selected : skipped, value ? skipped : selected);
  const source = observedProgram(arrow([], choose), call(id("worker")));
  assertWorkerResult(`generated-conditional-${seed}`, source, seed + 30);
}

// The temporal dead zone throws from the test and from the selected arm respectively.
// Returning normally through either expression would bypass the catch.
for (const [name, expr] of [
  ["test", "later ? 1 : 2"],
  ["arm", "true ? later : 2"],
]) {
  const source = `const worker = () => {
    try { return ${expr}; let later; } catch { return 7; }
  };
  export const result = worker();`;
  assertWorkerResult(`conditional-abrupt-${name}`, source, 7);
}

// The wrong comparison result throws during initialization, so an incorrect lowering
// cannot pass by making the same mistake in both the original and the split.
for (const [index, [left, op, right, expected]] of [
  [literal("1"), "===", literal(1), false],
  [id("worker"), "===", id("worker"), true],
  [id("worker"), "!==", { type: "ObjectExpression", properties: [] }, true],
  [id("NaN"), "===", id("NaN"), false],
].entries()) {
  const test = { type: "BinaryExpression", operator: op, left, right };
  const abrupt = call(arrow([], block([throws(literal(index))])));
  const result = conditional(test, expected ? literal(true) : abrupt, expected ? abrupt : literal(false));
  assertWorkerResult(
    `strict-comparison-${index}`,
    observedProgram({ type: "ObjectExpression", properties: [] }, result),
    expected
  );
}

// Exercise both direct evaluation and calls during initialization. The first seed
// reproduces the proof-cost regression on a moved worker returning "undefined" != typeof undefined.
for (const [index, [operand, tag, expected]] of [
  [id("undefined"), "undefined", false],
  [arrow([], literal(0)), "object", true],
  [literal(null), "object", false],
  [literal(3), "string", true],
].entries()) {
  const type = { type: "UnaryExpression", operator: "typeof", prefix: true, argument: operand };
  const [left, right] = index % 2 === 0 ? [literal(tag), type] : [type, literal(tag)];
  const expr = { type: "BinaryExpression", operator: "!=", left, right };
  const source = index < 2 ? observedProgram(arrow([], expr), call(id("worker"))) : observedProgram(expr, id("worker"));
  assertWorkerResult(`generated-string-inequality-${index}`, source, expected);
}

// Vary the number, spelling and values of static properties across a module boundary.
for (const seed of [0, 1, 2, 3, 14]) {
  const expected = {};
  const properties = Array.from({ length: seed }, (_, i) => {
    const key = i % 2 ? `quoted-key-${i}` : `field${i}`;
    expected[key] = i % 2 ? i : seed;
    const prop = property(key, i % 2 ? literal(i) : id("worker"));
    return i % 2 ? { ...prop, key: literal(key) } : prop;
  });
  assertWorkerResult(
    `generated-record-${seed}`,
    observedProgram(literal(seed), { type: "ObjectExpression", properties }),
    expected
  );
}

assertWorkerResult(
  "record-nested-values",
  `const worker = { source: ["first", { value: 42 }] };
  export const result = {
    nested: { field: worker.source[1].value },
    list: [worker.source[0], { key: worker.source.length }],
  };`,
  { nested: { field: 42 }, list: ["first", { key: 2 }] }
);

for (const [seed, key] of [id("value"), literal("quoted-key")].entries()) {
  const value = seed + 10;
  const provider = { type: "ObjectExpression", properties: [{ ...property("value", literal(value)), key }] };
  const read = {
    type: "MemberExpression",
    object: id("worker"),
    property: key,
    computed: key.type === "Literal",
    optional: false,
  };
  const list = { type: "ArrayExpression", elements: [read, { type: "ObjectExpression", properties: [] }] };
  const result = { type: "ObjectExpression", properties: [property("read", read), property("list", list)] };
  assertWorkerResult(`generated-record-values-${seed}`, observedProgram(provider, result), {
    read: value,
    list: [value, {}],
  });
}

// The first read throws a number. Reading the second value out of order
// throws a ReferenceError, which must not reach the catch instead.
assertWorkerResult(
  "record-value-order",
  `const worker = () => {
    const key = () => { throw 7; };
    try {
      return { first: ({})[key()], second: later.field };
      let later;
    } catch (error) {
      return typeof error;
    }
  };
  export const result = worker() === "number" ? "number" : (() => { throw 0; })();`,
  "number"
);

// Even the largest witness is insufficient for 62 properties. A throw after
// the first witness exhausts must instead report an abrupt initialization.
for (const [width, useLater, reason] of [
  [62, false, /exhausted the fuel search budget/],
  [14, true, /original threw during linked initialization/],
]) {
  const props = Array.from({ length: width }, (_, i) =>
    property(`field${i}`, useLater && i === width - 1 ? id("result") : literal(i))
  );
  const source = observedProgram(literal(0), { type: "ObjectExpression", properties: props });
  const { verdict, out } = workerSplit(`record-fuel-${width}`, source);
  assert.equal(verdict.status, "rejected");
  assert.match(verdict.reason, reason);
  assert(!existsSync(out));
}

// Vary substitution count and value type. The helper's parameter shadows the
// outer binding, and the escaped newline must come from the cooked segment.
for (const [value, expressions, expected] of [
  [0, [], "head\n"],
  [false, [id("input")], "head\nfalse/"],
  ["middle", [id("prefix"), id("input"), literal(null)], "head\ncaptured/middle/null/"],
]) {
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
  ["tagged-template", "const worker = value => value; export const result = worker`raw`;", /TaggedTemplateExpression/],
]) {
  const { verdict, out } = workerSplit(name, source);
  assert.equal(verdict.status, "rejected");
  assert.match(verdict.reason, reason);
  assert(!existsSync(out));
}

// Different numeric spellings compare by string contents.
assertWorkerResult("unequal-numeric-strings", 'const different = "01" != "1"; export const result = different;', true);
assertWorkerResult(
  "typeof-local-tdz",
  'const worker = () => { try { return "undefined" != typeof later; let later; } catch { return 7; } }; export const result = worker();',
  7
);
assertWorkerResult(
  "typeof-shadowed-host-name",
  'const window = null; export const result = "undefined" != typeof window;',
  true
);
for (const [name, expr, reason] of [
  ["record-prototype", "({ __proto__: null })", /unsupported __proto__/],
  ["record-computed", '({ ["key"]: 1 })', /static data property/],
  ["record-getter", "({ get key() { return 1; } })", /static data property/],
  ["record-spread", "({ ...worker })", /static data property/],
  ["record-duplicate", '({ key: 1, "key": 2 })', /duplicate object property/],
  ["record-direct-call", "({ key: worker() })", /unsupported object property value CallExpression/],
  ["record-anonymous-function", "({ key: () => 1 })", /unsupported object property value ArrowFunctionExpression/],
  ["record-array-hole", "({ nested: { key: [, 1] } })", /unsupported array hole/],
  ["record-optional-member", "({ key: worker?.field })", /unsupported object property value ChainExpression/],
  ["mixed-inequality", '"01" != 1', /requires two string operands/],
  ["host-typeof", '"undefined" != typeof window', /unsupported global window/],
]) {
  const { verdict, out } = workerSplit(name, `const worker = () => ${expr}; export const result = worker();`);
  assert.equal(verdict.status, "rejected");
  assert.match(verdict.reason, reason, name);
  assert(!existsSync(out));
}
