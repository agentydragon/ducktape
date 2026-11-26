import assert from "node:assert/strict";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { assertLinkedPreviewResult, prepareLinkedFixture, prepareWorkerFixture } from "./fixture_preview.mjs";
import {
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
  const fixture = prepareLinkedFixture(`generated-template-${value}`, source, {
    entry: "entry.mjs",
    modules: { "format.mjs": ["#0", "#1", "#2"], "entry.mjs": ["#3", "#4"] },
    order: ["format.mjs"],
  });
  assertPublicEmissionRefused(fixture.manifest, fixture.out);
  await assertLinkedPreviewResult(fixture, expected);
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
  const fixture = prepareWorkerFixture(`throwing-template-${values[0]}`, observedProgram(worker, call(id("worker"))));
  assertPublicEmissionRefused(fixture.manifest, fixture.out);
  await assertLinkedPreviewResult(fixture, values[0]);
}

if (runCase("template-object-coercion")) {
  const { manifest, out, sourceFile, specFile } = prepareWorkerFixture(
    "template-object-coercion",
    "const worker = value => `${value}`; export const result = worker({});"
  );
  assertPublicEmissionRefused(manifest, out);
  const expected = { result: "[object Object]" };
  assert.deepEqual(observe(sourceFile, "result"), expected);
  await withSplitPreview({ manifest }, ({ directory, entryFile }) => {
    assert.deepEqual(observe(entryFile, "result"), expected);
    const model = probeLinkedModel({ source: sourceFile, spec: specFile, directory });
    assert.equal(model.status, "refused", JSON.stringify(model));
    assert.equal(model.reason_code, "probe_unknown", JSON.stringify(model));
    assert.match(model.reason, /object is converted to a primitive/);
    assertPublicEmissionRefused(manifest, directory);
    assertPublicRecheckRefused(manifest, directory);
  });
}
