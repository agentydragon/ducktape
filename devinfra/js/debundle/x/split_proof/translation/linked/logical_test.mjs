import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  arrow,
  call,
  exportNamed,
  forcePreview,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  prepare,
  program,
  property,
  root,
  variable,
} from "./harness.mjs";

// Source order reads a later binding only when the left operand selects the right.
// In the emitted order that binding has already initialized.
for (const [operator, shortCircuit, evaluateRight] of [
  ["&&", "", "nonempty"],
  ["||", "present", 0],
  ["??", 0, null],
]) {
  for (const [left, readsLate] of [
    [shortCircuit, false],
    [evaluateRight, true],
  ]) {
    for (const inRecord of [false, true]) {
      const dir = path.join(root, operator + "-" + readsLate + "-" + inRecord);
      const spec = {
        entry: "entry.mjs",
        modules: { "use.mjs": ["#0"], "late.mjs": ["#1"], "entry.mjs": ["#2"] },
        order: ["late.mjs", "use.mjs"],
        sequential: true,
      };
      const right = operator === "||" ? call(arrow([], id("late"))) : id("late");
      const logical = { type: "LogicalExpression", operator, left: literal(left), right };
      const wrap = (v) => (inRecord ? { field: v } : v);
      const files = prepare(dir, {
        source: program([
          variable(
            "const",
            "value",
            inRecord ? { type: "ObjectExpression", properties: [property("field", logical)] } : logical
          ),
          variable("const", "late", literal("ready")),
          exportNamed([["value", "result"]]),
        ]),
        provider: "",
        spec,
      });
      const out = path.join(dir, "out");
      const { verdict, log } = lower(files.sourceFile, files.specFile, out);
      let candidate = out;
      if (readsLate) {
        assert.equal(verdict.status, "rejected", log);
        assert.match(verdict.reason, /original threw during linked initialization/);
        assert(!existsSync(out));
        assert.deepEqual(observe(files.sourceFile, "result"), { error: "ReferenceError" });
        candidate = path.join(dir, "preview");
        forcePreview(files, candidate);
      } else {
        assert.equal(verdict.status, "certified", log);
        assert.equal(verdict.strategy, "compiled-replay-premised", log);
        assert.deepEqual(observe(files.sourceFile, "result"), { result: wrap(left) });
      }
      assert.deepEqual(moduleEvaluationOrder(candidate, spec.entry), [...spec.order, spec.entry]);
      assert.deepEqual(observe(path.join(candidate, spec.entry), "result"), {
        result: wrap(readsLate ? "ready" : left),
      });
    }
  }
}

// The right operand also replays when it is reached and returns a primitive.
for (const [operator, left, right] of [
  ["&&", true, "and"],
  ["||", "", "or"],
  ["??", null, "nullish"],
]) {
  const dir = path.join(root, `selected-right-${operator}`);
  const files = prepare(dir, {
    source: program([
      variable("const", "value", { type: "LogicalExpression", operator, left: literal(left), right: literal(right) }),
      variable("const", "late", literal("ready")),
      exportNamed([["value", "result"]]),
    ]),
    provider: "",
    spec: {
      entry: "entry.mjs",
      modules: { "use.mjs": ["#0"], "late.mjs": ["#1"], "entry.mjs": ["#2"] },
      order: ["late.mjs", "use.mjs"],
      sequential: true,
    },
  });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.strategy, "compiled-replay-premised", log);
  for (const file of [files.sourceFile, path.join(out, "entry.mjs")]) {
    assert.deepEqual(observe(file, "result"), { result: right });
  }
}

// Reading a function through || gives it no property-inferred name.
{
  const dir = path.join(root, "record-fallback");
  const files = prepare(dir, {
    source: `const record = {field: null || function () {}};
             export const observed = record.field.name;`,
    provider: "",
    spec: { entry: "entry.mjs", modules: { "config.mjs": ["#0"], "entry.mjs": ["#1"] }, order: ["config.mjs"] },
  });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.strategy, "generated-proof", log);
  for (const file of [files.sourceFile, path.join(out, "entry.mjs")]) {
    assert.deepEqual(observe(file, "observed"), { observed: "" });
  }
}
