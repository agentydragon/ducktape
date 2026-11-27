import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { split } from "../testing/split.mjs";
import {
  exportNamed,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  prepare,
  program,
  root,
  variable,
} from "./harness.mjs";

const cases = [
  ["&&", "", "nonempty", "and"],
  ["||", "present", 0, "or"],
  ["??", 0, null, "nullish"],
];

// A selected right operand reads a later lexical binding in the source. Moving
// that binding before the read must not turn the TDZ failure into success.
for (const [operator, , evaluateRight] of cases) {
  const dir = path.join(root, `tdz-${operator}`);
  const files = prepare(dir, {
    source: program([
      variable("const", "value", {
        type: "LogicalExpression",
        operator,
        left: literal(evaluateRight),
        right: id("late"),
      }),
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
  assert.deepEqual(observe(files.sourceFile, "result"), { error: "ReferenceError" });
  const out = path.join(dir, "out");
  const request = { source: files.sourceFile, spec: files.specFile, out, require: "all-outcomes" };
  const refused = split(request);
  assert.equal(refused.status, "refused", refused.log);
  assert(!existsSync(out));
  mkdirSync(out);
  const sentinel = path.join(out, "sentinel.txt");
  writeFileSync(sentinel, "keep these bytes\n");
  const existing = split({ ...request, allowExistingOut: true });
  assert.equal(existing.status, "refused", existing.log);
  assert.equal(readFileSync(sentinel, "utf8"), "keep these bytes\n");
}

// TODO: Revisit skipped closure and record operands if logical reordering expands.

// An independent move takes the slice first. Both graphs evaluate the selected
// literal operand, and the unrelated declaration may move before it.
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
  assert.equal(verdict.strategy, "compiled-slice", log);
  assert.deepEqual(moduleEvaluationOrder(out, "entry.mjs"), ["late.mjs", "use.mjs", "entry.mjs"]);
  for (const file of [files.sourceFile, path.join(out, "entry.mjs")]) {
    assert.deepEqual(Object.entries(await import(pathToFileURL(file).href)), [["result", right]]);
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
