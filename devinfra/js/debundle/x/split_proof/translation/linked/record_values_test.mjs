import assert from "node:assert/strict";
import path from "node:path";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import {
  arrow,
  binary,
  block,
  call,
  conditional,
  exportNamed,
  id,
  literal,
  member,
  observe,
  prepare,
  program,
  property,
  returns,
  root,
  throws,
  unary,
  variable,
} from "./harness.mjs";

const readProperty = (object, key) => member(object, literal(key), true);
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
    ["flag", unary("!", literal(operand)), !operand],
    ["matches", binary("===", literal(operand), literal(seed)), operand === seed],
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
  const read = (name) => readProperty(id("settings"), name);
  const check = (actual, expected) => ({
    type: "IfStatement",
    test: binary("!==", actual, expected),
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
    checks.push(check(readProperty(read(name), "name"), literal(expected)));

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
  const manifest = writeDeployment(files.sourceFile, files.specFile, out);
  assertPublicEmissionRefused(manifest, out);
  await withSplitPreview({ source: files.sourceFile, spec: files.specFile }, async ({ directory, entryFile }) => {
    const model = probeLinkedModel({
      source: files.sourceFile,
      spec: files.specFile,
      directory,
      observe: [["result"]],
    });
    const bits = ["0", "4607182418800017408", "4611686018427387904", "4613937818241073152"][seed];
    assert.equal(model.status, "matched");
    assert(model.fuel > 0);
    assert.deepEqual(model.assumptions, []);
    assert.deepEqual(model.host_refs, []);
    assert.deepEqual(model.observed, [{ path: ["result"], original: `#${bits}`, split: `#${bits}` }]);
    if (seed === 0) {
      assertPublicEmissionRefused(manifest, directory);
      assertPublicRecheckRefused(manifest, directory);
    }
    for (const file of [files.sourceFile, entryFile]) assert.deepEqual(observe(file, "result"), { result: seed });
  });
}
