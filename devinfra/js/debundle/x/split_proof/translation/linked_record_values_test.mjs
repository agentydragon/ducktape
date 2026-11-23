import assert from "node:assert/strict";
import path from "node:path";
import {
  arrow,
  block,
  call,
  conditional,
  exportNamed,
  id,
  literal,
  lower,
  observe,
  prepare,
  program,
  property,
  returns,
  root,
  throws,
  variable,
} from "./linked_programs_test_support.mjs";

const member = (object, key) => ({ type: "MemberExpression", object, property: literal(key), computed: true });
const equal = (left, right) => ({ type: "BinaryExpression", operator: "===", left, right });
const field = (key, value) => ({ ...property(key, value), key: literal(key) });

for (const [seed, operand] of [0, 1, "ready", null].entries()) {
  const key = `handler-${seed}`;
  const thunk = arrow([], literal(seed));
  const template = {
    type: "TemplateLiteral",
    expressions: [literal(seed)],
    quasis: ["v", ""].map((text, i) => ({
      type: "TemplateElement",
      value: { raw: text, cooked: text },
      tail: i === 1,
    })),
  };
  const specs = [
    ["flag", { type: "UnaryExpression", operator: "!", argument: literal(operand), prefix: true }, !operand],
    ["matches", equal(literal(operand), literal(seed)), operand === seed],
    ["choice", conditional(literal(operand), literal(1), literal(2)), operand ? 1 : 2],
    ["label", template, `v${seed}`],
    ["receiver", { type: "ThisExpression" }, undefined],
  ];
  const record = {
    type: "ObjectExpression",
    properties: [
      ...specs.map(([name, value]) => field(name, value)),
      field(key, thunk),
      field("anonymous", { type: "FunctionExpression", id: null, params: [], body: block([]) }),
      field("explicit", { type: "FunctionExpression", id: id("named"), params: [], body: block([]) }),
      field("indirect", { type: "SequenceExpression", expressions: [literal(0), thunk] }),
    ],
  };
  const read = (name) => member(id("settings"), name);
  const check = (actual, expected) => ({
    type: "IfStatement",
    test: { type: "BinaryExpression", operator: "!==", left: actual, right: expected },
    consequent: throws(literal("wrong property value")),
    alternate: null,
  });
  // Make the observations during initialization so a wrong result in CoreJS prevents certification.
  const checks = specs.map(([name, , expected]) =>
    check(read(name), expected === undefined ? id("undefined") : literal(expected))
  );
  for (const [name, expected] of [
    [key, key],
    ["anonymous", "anonymous"],
    ["explicit", "named"],
    ["indirect", ""],
  ])
    checks.push(check(member(read(name), "name"), literal(expected)));

  const source = program([
    variable("const", "settings", record),
    variable("const", "result", call(arrow([], block([...checks, returns(call(read(key)))])))),
    exportNamed([["result", "result"]]),
  ]);
  const dir = path.join(root, `record-values-${seed}`);
  const entry = "entry.mjs";
  const files = prepare(dir, {
    source,
    provider: "",
    spec: {
      entry,
      modules: { "settings.mjs": ["#0"], [entry]: ["#1", "#2"] },
      order: ["settings.mjs"],
    },
  });
  const out = path.join(dir, "out");
  for (const options of seed === 0 ? [[], ["--check-output"]] : [[]]) {
    const { verdict, log } = lower(files.sourceFile, files.specFile, out, options);
    assert.equal(verdict.status, "certified", log);
    assert.equal(verdict.guarantee, "normal-completion", log);
  }
  for (const file of [files.sourceFile, path.join(out, entry)])
    assert.deepEqual(observe(file, "result"), { result: seed });
}
