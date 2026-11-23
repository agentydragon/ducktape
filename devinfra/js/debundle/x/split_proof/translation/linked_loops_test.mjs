import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  arrow,
  block,
  call,
  exportNamed,
  expression,
  forcePreview,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  program,
  returns,
  sequentialSplit,
  variable,
} from "./testing/linked_programs.mjs";

const entry = "entry.mjs";
// The update gets a fresh lexical cell. A closure made before continue must still
// see the first iteration's value, even after the update and the second iteration.
const capture = sequentialSplit(
  "iteration-capture",
  `const capture = () => {
    let first;
    for (let more = true; ; more = false) {
      if (more) { first = () => more; continue; }
      break;
    }
    return first();
  }; export const result = capture();`,
  { "capture.mjs": ["#0"], [entry]: ["#1"] },
  ["capture.mjs"]
);
assert.equal(capture.verdict.status, "certified", capture.log);
assert.equal(
  lower(capture.files.sourceFile, capture.files.specFile, capture.out, ["--check-output"]).verdict.status,
  "certified"
);
for (const file of [capture.files.sourceFile, path.join(capture.out, entry)]) {
  assert.deepEqual(observe(file, "result"), { result: true });
}

const write = (name, right) => ({ type: "AssignmentExpression", operator: "=", left: id(name), right });
const jump = (type) => ({ type, label: null });
for (const kind of ["ForStatement", "WhileStatement"]) {
  for (const dependent of [false, true]) {
    const turn = id("turn");
    const advance = write("turn", literal(false));
    const loop = {
      type: kind,
      ...(kind === "ForStatement" ? { init: write("turn", literal(true)), update: advance } : {}),
      test: literal(true),
      body: block([
        {
          type: "IfStatement",
          test: turn,
          consequent: block([...(kind === "WhileStatement" ? [expression(advance)] : []), jump("ContinueStatement")]),
          alternate: null,
        },
        jump("BreakStatement"),
      ]),
    };
    const source = program([
      variable("const", "ready", literal(kind)),
      variable(
        "const",
        "result",
        call(
          arrow(
            [],
            block([variable("let", "turn", literal(true)), loop, returns(dependent ? id("ready") : literal(kind))])
          )
        )
      ),
      exportNamed([["result", "result"]]),
    ]);
    const order = ["result.mjs", "ready.mjs"];
    const test = sequentialSplit(
      kind + dependent,
      source,
      { "ready.mjs": ["#0"], "result.mjs": ["#1"], [entry]: ["#2"] },
      order
    );
    assert.deepEqual(observe(test.files.sourceFile, "result"), { result: kind });
    let candidate = test.out;
    if (dependent) {
      assert.equal(test.verdict.status, "rejected", test.log);
      assert(!existsSync(test.out));
      candidate = path.join(test.dir, "preview");
      forcePreview(test.files, candidate);
    } else {
      assert.equal(test.verdict.status, "certified", test.log);
    }
    assert.deepEqual(moduleEvaluationOrder(candidate, entry), [...order, entry]);
    assert.deepEqual(
      observe(path.join(candidate, entry), "result"),
      dependent ? { error: "ReferenceError" } : { result: kind }
    );
  }
}
