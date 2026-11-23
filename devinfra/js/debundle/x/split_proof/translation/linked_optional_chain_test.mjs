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
} from "./linked_programs_test_support.mjs";

const entry = "entry.mjs";
const owner = "owner.mjs";
const reader = "reader.mjs";
const state = "state.mjs";

function split(
  name,
  source,
  modules = { [owner]: ["#0"], [reader]: ["#1"], [entry]: ["#2"] },
  order = [owner, reader]
) {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider: "", spec: { entry, modules, order, sequential: true } });
  const out = path.join(dir, "out");
  return { files, dir, out, ...lower(files.sourceFile, files.specFile, out) };
}

function assertCertified({ files, verdict, log, out }, result) {
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", log);
  assert.deepEqual(observe(files.sourceFile, "result"), { result });
  assert.deepEqual(observe(path.join(out, entry), "result"), { result });
}

function assertRejected({ verdict, log, out }, reason) {
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, reason, log);
  assert(!existsSync(out));
}

// A nullish base skips the key. A reached key can rebind the source cell
// without changing the selected getter's receiver.
assertCertified(
  split(
    "computed-key-effects",
    `const run = () => {
      let trace = "";
      const record = {
        value: "original",
        get selected() {
          trace = trace + "g";
          return this.value;
        }
      };
      let target = null;
      const key = () => {
        trace = trace + "k";
        target = { value: "new" };
        return "selected";
      };
      if (target?.[key()] !== undefined || trace !== "") throw "null computed the key";
      target = undefined;
      if (target?.[key()] !== undefined || trace !== "") throw "undefined computed the key";
      target = record;
      const result = target?.[key()];
      if (result !== "original" || trace !== "kg" || target.value !== "new") throw "incorrect property read";
      return [result, trace];
    };
    const result = run();
    export { result };`
  ),
  ["original", "kg"]
);

function optionalMember(object, property, computed) {
  return {
    type: "ChainExpression",
    expression: { type: "MemberExpression", object, property, computed, optional: true },
  };
}

const generated = program([
  variable(
    "const",
    "read",
    arrow([id("target"), id("key")], {
      type: "ArrayExpression",
      elements: [optionalMember(id("target"), id("value"), false), optionalMember(id("target"), id("key"), true)],
    })
  ),
  variable("const", "result", {
    type: "ArrayExpression",
    elements: [
      literal(null),
      id("undefined"),
      literal(false),
      { type: "ObjectExpression", properties: [property("value", literal(0)), property("other", literal("present"))] },
    ].map((target) => call(id("read"), [target, literal("other")])),
  }),
  exportNamed([["result", "result"]]),
]);
assertCertified(split("generated-optional-reads", generated), [
  [null, null],
  [null, null],
  [null, null],
  [0, "present"],
]);

const reordered = `function read() { return current?.value ?? "missing"; }
  var current;
  current = { value: "ready" };
  const result = read();
  export { result };`;
const placements = { [owner]: ["#0"], [state]: ["#1", "#2"], [reader]: ["#3"], [entry]: ["#4"] };
assertCertified(split("ready-before-read", reordered, placements, [owner, state, reader]), "ready");
const earlyOrder = [owner, reader, state];
const early = split("read-before-ready", reordered, placements, earlyOrder);
assertRejected(early, /probe executions completed normally with different final states/);
const preview = path.join(early.dir, "preview");
forcePreview(early.files, preview);
assert.deepEqual(moduleEvaluationOrder(preview, entry), [...earlyOrder, entry]);
assert.deepEqual(observe(early.files.sourceFile, "result"), { result: "ready" });
assert.deepEqual(observe(path.join(preview, entry), "result"), { result: "missing" });

for (const [name, body, reason, expected] of [
  ["parenthesized-method", "(target?.method)()", /unsupported optional chain as a call target/, 42],
  ["optional-method", "target?.method()", /unsupported optional chain/, 42],
  ["continued-member", "target?.record.value", /unsupported optional chain/, 17],
  ["delete-chain", "delete target?.value", /delete/, true],
]) {
  const test = split(
    name,
    `const run = (target) => ${body};
      const result = run({ value: 42, method() { return this.value; }, record: { value: 17 } });
      export { result };`
  );
  assert.deepEqual(observe(test.files.sourceFile, "result"), { result: expected });
  assertRejected(test, reason);
}
