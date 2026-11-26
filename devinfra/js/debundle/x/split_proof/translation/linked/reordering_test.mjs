import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  assertCertified,
  assertNoCompiledRule,
  root,
  id,
  literal,
  call,
  returns,
  block,
  tryStatement,
  catchClause,
  arrow,
  variable,
  property,
  exportNamed,
  program,
  observe,
  lower,
  prepare,
  moduleEvaluationOrder,
  forcePreview,
} from "./harness.mjs";

// Source initializes left, then right. Pin the reverse order across all cases.
// Mutating one initializer to read its neighbor introduces a TDZ in exactly one execution.
for (const [changed, first, second] of [
  ["left", "low", "high"],
  ["right", 42, 0],
]) {
  const spec = {
    entry: "entry.mjs",
    modules: { "left.mjs": ["#0"], "right.mjs": ["#1"], "entry.mjs": ["#2", "#3"] },
    order: ["right.mjs", "left.mjs"],
    sequential: true,
  };
  for (const dependent of [false, true]) {
    const dir = path.join(root, `reordered-providers-${changed}-${dependent}`);
    const readsRight = dependent && changed === "left";
    const readsLeft = dependent && changed === "right";
    const files = prepare(dir, {
      source: program([
        variable("const", "left", readsRight ? id("right") : literal(first)),
        variable("const", "right", readsLeft ? id("left") : literal(second)),
        variable("const", "pair", {
          type: "ObjectExpression",
          properties: [property("first", id("left")), property("second", id("right"))],
        }),
        exportNamed([["pair", "result"]]),
      ]),
      provider: "",
      spec,
    });
    const out = path.join(dir, "out");
    const { verdict, log } = lower(files.sourceFile, files.specFile, out);
    const tdz = { error: "ReferenceError" };
    const normal = { result: { first: readsRight ? second : first, second: readsLeft ? first : second } };
    assert.deepEqual(observe(files.sourceFile, "result"), readsRight ? tdz : normal);
    let candidate = out;
    if (dependent) {
      assert.equal(verdict.status, "rejected", log);
      assert.equal(verdict.reason, `${readsRight ? "original" : "split"} threw during linked initialization`);
      assert(!existsSync(out));
      candidate = path.join(dir, "preview");
      forcePreview(files, candidate);
    } else {
      assertCertified(verdict, log);
    }
    assert.deepEqual(moduleEvaluationOrder(candidate, spec.entry), [...spec.order, spec.entry]);
    assert.deepEqual(observe(path.join(candidate, spec.entry), "result"), readsLeft ? tdz : normal);
  }
}

// Both calls can complete even when the formatter observes their order: catch
// the TDZ and return the usual formatted value until the captured call finishes.
for (const [observed, leftArg, rightArg] of [
  ["left", "sun", "rain"],
  ["right", 13, false],
]) {
  const format = {
    type: "TemplateLiteral",
    expressions: [id("prefix"), id("value")],
    quasis: ["", ":", ""].map((text, index) => ({
      type: "TemplateElement",
      value: { raw: text, cooked: text },
      tail: index === 2,
    })),
  };
  const left = `label:${leftArg}`;
  const right = `label:${rightArg}`;
  const original = { left, right };
  const shared = observed === "left" ? left : right;
  const dependentSource = observed === "left" ? { left, right: shared } : original;
  const dependentReversed = observed === "right" ? { left: shared, right } : original;
  assert.notDeepEqual(dependentSource, dependentReversed);

  for (const [variant, capturesCall, reversed] of [
    ["independent", false, true],
    ["same-order", true, false],
    ["order-sensitive", true, true],
  ]) {
    const body = capturesCall
      ? block([tryStatement(block([returns(id(observed))]), catchClause(null, block([returns(format)])))])
      : format;
    const source = program([
      variable("const", "prefix", literal("label")),
      variable("const", "format", arrow([id("value")], body)),
      variable("const", "left", call(id("format"), [literal(leftArg)])),
      variable("const", "right", call(id("format"), [literal(rightArg)])),
      variable("const", "pair", {
        type: "ObjectExpression",
        properties: [property("left", id("left")), property("right", id("right"))],
      }),
      exportNamed([["pair", "result"]]),
    ]);
    const calls = reversed ? ["right.mjs", "left.mjs"] : ["left.mjs", "right.mjs"];
    const spec = {
      entry: "entry.mjs",
      modules: { "format.mjs": ["#0", "#1"], "left.mjs": ["#2"], "right.mjs": ["#3"], "entry.mjs": ["#4", "#5"] },
      order: ["format.mjs", ...calls],
      sequential: true,
    };
    const dir = path.join(root, `reordered-formatter-${observed}-${variant}`);
    const files = prepare(dir, { source, provider: "", spec });
    const out = path.join(dir, "out");
    const { verdict, log } = lower(files.sourceFile, files.specFile, out);
    const bundled = capturesCall ? dependentSource : original;
    assert.deepEqual(observe(files.sourceFile, "result"), { result: bundled }, log);

    let candidate = out;
    if (capturesCall && reversed) {
      assert.equal(verdict.status, "rejected", log);
      assert.match(verdict.reason, /probe executions completed normally with different final states/, log);
      assert(!existsSync(out));
      candidate = path.join(dir, "preview");
      forcePreview(files, candidate);
    } else {
      assertCertified(verdict, log);
    }
    assert.deepEqual(moduleEvaluationOrder(candidate, spec.entry), [...spec.order, spec.entry], log);
    const expected = capturesCall && reversed ? dependentReversed : bundled;
    assert.deepEqual(observe(path.join(candidate, spec.entry), "result"), { result: expected }, log);
  }
}

// These distinct world calls both return undefined in World.probe, so the concrete
// final states match. Other worlds can distinguish the order of these calls.
// The compiled checker must refuse the reversed schedule.
{
  const localeCompare = (left, right) =>
    call(
      {
        type: "MemberExpression",
        object: literal(left),
        property: id("localeCompare"),
        computed: false,
        optional: false,
      },
      [literal(right)]
    );
  const source = program([
    variable("const", "first", localeCompare("x", "y")),
    variable("const", "second", localeCompare("z", "w")),
    exportNamed([
      ["first", "first"],
      ["second", "second"],
    ]),
  ]);
  const spec = {
    entry: "entry.mjs",
    modules: { "left.mjs": ["#0"], "right.mjs": ["#1"], "entry.mjs": ["#2"] },
    order: ["right.mjs", "left.mjs"],
    sequential: true,
  };
  const dir = path.join(root, "reordered-probe-equivalent-host-calls");
  const files = prepare(dir, { source, provider: "", spec });
  const preview = path.join(dir, "preview");
  assertNoCompiledRule(files, preview);
  assert.deepEqual(moduleEvaluationOrder(preview, spec.entry), [...spec.order, spec.entry]);
}
