import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  arrow,
  binary,
  call,
  exportNamed,
  forcePreview,
  id,
  literal,
  lower,
  member,
  moduleEvaluationOrder,
  observe,
  prepare,
  program,
  property,
  root,
  variable,
} from "./testing/linked_programs.mjs";

const entry = "entry.mjs";
const owner = "owner.mjs";
const observer = "observer.mjs";
const oneCall = { [owner]: ["#0"], [observer]: ["#1"], [entry]: ["#2"] };

function split(name, source, modules = oneCall, order = [owner, observer]) {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider: "", spec: { entry, modules, order, sequential: true } });
  const out = path.join(dir, "out");
  return { files, dir, out, ...lower(files.sourceFile, files.specFile, out) };
}

function assertCertified({ files, out, verdict, log }, result) {
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", log);
  assert.deepEqual(observe(files.sourceFile, "result"), { result });
  assert.deepEqual(observe(path.join(out, entry), "result"), { result });
}

const position = `function position(index, values) {
  return values.length === 1 ? "single" : index === 0 ? "first" : index === values.length - 1 ? "last" : "middle";
}`;

// The exported Boolean records an eager call through the split's import.
assertCertified(
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
  assertCertified(split(name, source), result);
}

// Both schedules finish. In the forced split, the eager call sees the hoisted
// length cell before the write, so its subtraction produces NaN.
{
  const source = `${position}
    var length;
    length = " 3 ";
    const result = position(2, { length }) === "last";
    export { result };`;
  const state = "state.mjs";
  const modules = { [owner]: ["#0"], [state]: ["#1", "#2"], [observer]: ["#3"], [entry]: ["#4"] };
  assertCertified(split("after-length-write", source, modules, [owner, state, observer]), true);

  const order = [owner, observer, state];
  const early = split("before-length-write", source, modules, order);
  assert.equal(early.verdict.status, "rejected", early.log);
  assert.match(early.verdict.reason, /probe executions completed normally with different final states/, early.log);
  assert(!existsSync(early.out));
  const preview = path.join(early.dir, "preview");
  forcePreview(early.files, preview);
  assert.deepEqual(moduleEvaluationOrder(preview, entry), [...order, entry]);
  assert.deepEqual(observe(early.files.sourceFile, "result"), { result: true });
  assert.deepEqual(observe(path.join(preview, entry), "result"), { result: false });
}
