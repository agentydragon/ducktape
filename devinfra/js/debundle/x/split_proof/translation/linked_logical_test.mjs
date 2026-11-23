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
  root,
  variable,
} from "./linked_programs_test_support.mjs";

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
    const dir = path.join(root, operator + "-" + readsLate);
    const spec = {
      entry: "entry.mjs",
      modules: { "use.mjs": ["#0"], "late.mjs": ["#1"], "entry.mjs": ["#2"] },
      order: ["late.mjs", "use.mjs"],
      sequential: true,
    };
    const right = operator === "||" ? call(arrow([], id("late"))) : id("late");
    const files = prepare(dir, {
      source: program([
        variable("const", "value", { type: "LogicalExpression", operator, left: literal(left), right }),
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
      assert.deepEqual(observe(files.sourceFile, "result"), { result: left });
    }
    assert.deepEqual(moduleEvaluationOrder(candidate, spec.entry), [...spec.order, spec.entry]);
    assert.deepEqual(observe(path.join(candidate, spec.entry), "result"), { result: readsLate ? "ready" : left });
  }
}
