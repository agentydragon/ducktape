import assert from "node:assert/strict";
import {
  arrow,
  assertNoConstructedRule,
  binary,
  call,
  exportNamed,
  id,
  literal,
  member,
  observe,
  sequentialSplit,
  program,
  property,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const owner = "owner.mjs";
const observer = "observer.mjs";
const oneCall = { [owner]: ["#0"], [observer]: ["#1"], [entry]: ["#2"] };

function split(name, source, modules = oneCall, order = [owner, observer]) {
  return sequentialSplit(name, source, modules, order);
}

function assertOriginalOnly(test, result) {
  assertNoConstructedRule(test);
  assert.deepEqual(observe(test.files.sourceFile, "result"), { result }, test.files.sourceFile);
}

const position = `function position(index, values) {
  return values.length === 1 ? "single" : index === 0 ? "first" : index === values.length - 1 ? "last" : "middle";
}`;

// This function declaration is outside the current constructor's admitted form.
// Keep the original Node result as a source-semantics check and require refusal.
assertOriginalOnly(
  split(
    "position-observer",
    `${position}
    const result = position(2, { length: " 3 " }) === "last";
    export { result };`
  ),
  true
);

const lengthRead = member(id("values"), id("length"));

for (const [name, length, index, result] of [
  ["numeric-length", 3, 2, true],
  ["string-length", " 0x3 ", 2, true],
  ["strict-index", " 3 ", "2", false],
]) {
  const source = program([
    variable(
      "const",
      "isLast",
      arrow([id("index"), id("values")], binary("===", id("index"), binary("-", lengthRead, literal(1))))
    ),
    variable(
      "const",
      "result",
      call(id("isLast"), [
        literal(index),
        { type: "ObjectExpression", properties: [property("length", literal(length))] },
      ])
    ),
    exportNamed([["result", "result"]]),
  ]);
  assertOriginalOnly(split(name, source), result);
}

// The source result remains checked in Node, while the reordered request is
// refused because this program is outside the named-declaration constructor.
{
  const source = `${position}
    var length;
    length = " 3 ";
    const result = position(2, { length }) === "last";
    export { result };`;
  const state = "state.mjs";
  const modules = { [owner]: ["#0"], [state]: ["#1", "#2"], [observer]: ["#3"], [entry]: ["#4"] };
  assertOriginalOnly(split("after-length-write", source, modules, [owner, state, observer]), true);

  const order = [owner, observer, state];
  const early = split("before-length-write", source, modules, order);
  assertOriginalOnly(early, true);
}
