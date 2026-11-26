import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
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

const cases = [
  ["&&", "", "nonempty", "and"],
  ["||", "present", 0, "or"],
  ["??", 0, null, "nullish"],
];

// Source order reads a later binding only when the left operand selects the right.
// In the emitted order that binding has already initialized.
for (const [operator, shortCircuit, evaluateRight] of cases) {
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

// An independent move takes the slice first. In the replay control the
// reached RHS has an untaken read of the moved binding, so the slice's static
// move proof cannot justify the same inversion.
for (const [operator, left, right] of [
  ["&&", true, "and"],
  ["||", "", "or"],
  ["??", null, "nullish"],
]) {
  for (const replay of [false, true]) {
    const kind = replay ? "selected-right-replay" : "selected-right";
    const dir = path.join(root, `${kind}-${operator}`);
    const operand = replay
      ? {
          type: "LogicalExpression",
          operator: operator === "??" ? "??" : "||",
          left: literal(right),
          right: id("late"),
        }
      : literal(right);
    const files = prepare(dir, {
      source: program([
        variable("const", "value", { type: "LogicalExpression", operator, left: literal(left), right: operand }),
        variable("const", "late", literal("ready")),
        {
          type: "ExportNamedDeclaration",
          declaration: variable("const", "result", id("value")),
          specifiers: [],
          source: null,
        },
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
    assert.equal(verdict.strategy, replay ? "compiled-replay-premised" : "compiled-slice", log);
    for (const file of [files.sourceFile, path.join(out, "entry.mjs")]) {
      assert.deepEqual(Object.entries(await import(pathToFileURL(file).href)), [["result", right]]);
    }
  }
}

// Both graphs must evaluate the selected right operand exactly once.
for (const [operator, shortCircuit, evaluateRight, right] of cases) {
  for (const [left, hits] of [
    [shortCircuit, 0],
    [evaluateRight, 1],
  ]) {
    const dir = path.join(root, `right-effect-${operator}-${hits}`);
    const files = prepare(dir, {
      source: `let count = 0;
        const value = ${JSON.stringify(left)} ${operator} (count = count + 1, ${JSON.stringify(right)});
        export const hits = count;
        export const result = value;`,
      provider: "",
      spec: {
        entry: "entry.mjs",
        modules: { "value.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3"] },
        order: ["value.mjs"],
        sequential: true,
      },
    });
    const out = path.join(dir, "out");
    const { verdict, log } = lower(files.sourceFile, files.specFile, out, [], "linked-slice-order-kept");
    assert.equal(verdict.status, "certified", log);
    assert.equal(verdict.strategy, "compiled-slice", log);
    for (const file of [files.sourceFile, path.join(out, "entry.mjs")]) {
      assert.deepEqual(Object.entries(await import(pathToFileURL(file).href)), [
        ["hits", hits],
        ["result", hits === 0 ? left : right],
      ]);
    }
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
  assert.equal(verdict.strategy, "compiled-slice", log);
  assert.equal(verdict.scope, "linked-slice-order-kept", log);
  for (const file of [files.sourceFile, path.join(out, "entry.mjs")]) {
    assert.deepEqual(Object.entries(await import(pathToFileURL(file).href)), [["observed", ""]]);
  }
}
