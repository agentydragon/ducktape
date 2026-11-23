import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  arrow,
  block,
  call,
  exportNamed,
  forcePreview,
  functionExpression,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  prepare,
  program,
  property,
  returns,
  root,
  variable,
} from "./testing/linked_programs.mjs";

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

// An evaluated receiver runs once per call. Only the reached call looks up a
// method and computes the argument, in that order.
const evaluated = split(
  "optional-method-evaluated-receiver",
  `const run = () => {
    let trace = "";
    let current = null;
    const holder = { get parent() { trace += "p"; return current; } };
    const argument = () => { trace += "a"; };
    const skipped = holder.parent?.method(argument()) === undefined;
    current = {
      value: 42,
      get method() {
        trace += "g";
        return function () { return this.value; };
      }
    };
    const result = holder.parent?.method(argument());
    return [skipped, result, trace];
  };
  const result = run();
  export { result };`
);
assertCertified(evaluated, [true, 42, "ppga"]);
assert.deepEqual(
  lower(evaluated.files.sourceFile, evaluated.files.specFile, evaluated.out, ["--check-output"]).verdict,
  evaluated.verdict
);

// Changing the lexical binding during argument evaluation leaves the original
// receiver in the call. An undefined receiver skips that change altogether.
assertCertified(
  split(
    "optional-method-lexical-receiver",
    `const run = () => {
      let target;
      const argument = () => { target = null; return 2; };
      const skipped = target?.method(argument()) === undefined && target === undefined;
      target = { value: 5, method(delta) { return this.value + delta; } };
      const result = target?.method(argument());
      return [skipped, result, target === null];
    };
    const result = run();
    export { result };`
  ),
  [true, 7, true]
);

const optionalMethod = (object, method, args) => ({
  type: "ChainExpression",
  expression: {
    type: "CallExpression",
    callee: optionalMember(object, id(method), false).expression,
    arguments: args,
    optional: false,
  },
});
const method = {
  ...property(
    "method",
    functionExpression(
      [],
      block([
        returns({
          type: "MemberExpression",
          object: { type: "ThisExpression" },
          property: id("value"),
          computed: false,
          optional: false,
        }),
      ])
    )
  ),
  method: true,
};
const generatedCalls = program([
  variable("const", "invoke", arrow([id("receiver")], optionalMethod(id("receiver"), "method", []))),
  variable("const", "result", {
    type: "ArrayExpression",
    elements: [
      literal(null),
      { type: "ObjectExpression", properties: [method, property("value", literal(false))] },
    ].map((base) => call(id("invoke"), [base])),
  }),
  exportNamed([["result", "result"]]),
]);
assertCertified(split("generated-optional-method-calls", generatedCalls), [null, false]);

const missingMethod = split(
  "optional-method-falsy-receiver",
  `const run = (target) => target?.method();
    const result = run(false);
    export { result };`
);
assertRejected(missingMethod, /original threw during linked initialization/);
assert.deepEqual(observe(missingMethod.files.sourceFile, "result"), { error: "TypeError" });

const uninitialized = split(
  "optional-method-tdz",
  `const run = () => {
    const result = receiver?.method();
    const receiver = { method() { return 7; } };
    return result;
  };
  const result = run();
  export { result };`
);
assertRejected(uninitialized, /original threw during linked initialization/);
assert.deepEqual(observe(uninitialized.files.sourceFile, "result"), { error: "ReferenceError" });

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
  ["optional-receiver-and-callee", "target?.method?.()", /unsupported optional call/, 42],
  ["computed-optional-method", 'target?.["method"]()', /unsupported computed optional method call/, 42],
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
