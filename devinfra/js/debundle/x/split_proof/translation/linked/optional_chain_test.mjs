import assert from "node:assert/strict";
import path from "node:path";
import {
  arrow,
  assertCertifiedResult,
  assertRejected,
  assertRejectedOrOrderKeptSlice,
  block,
  call,
  caseShard,
  exportNamed,
  forcePreview,
  functionExpression,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  ownerReaderSplit,
  program,
  property,
  returns,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const owner = "owner.mjs";
const reader = "reader.mjs";
const state = "state.mjs";

function optionalMember(object, property, computed) {
  return {
    type: "ChainExpression",
    expression: { type: "MemberExpression", object, property, computed, optional: true },
  };
}

const runCase = caseShard();

if (runCase("computed-key-effects")) {
  // A nullish base skips the key. A reached key can rebind the source cell
  // without changing the selected getter's receiver.
  assertCertifiedResult(
    ownerReaderSplit(
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
}

if (runCase("consecutive-optional-reads")) {
  assertCertifiedResult(
    ownerReaderSplit(
      "consecutive-optional-reads",
      `const run = () => {
      let trace = "";
      const key = () => { trace += "k"; return "value"; };
      const record = { value: 7, get child() { trace += "g"; return this; } };
      const read = (base) => base?.child?.[key()];
      return [read(null), read({ child: null }), read(record), trace];
    };
    const result = run();
    export { result };`
    ),
    [null, null, 7, "gk"]
  );
}

if (runCase("generated-optional-reads")) {
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
        {
          type: "ObjectExpression",
          properties: [property("value", literal(0)), property("other", literal("present"))],
        },
      ].map((target) => call(id("read"), [target, literal("other")])),
    }),
    exportNamed([["result", "result"]]),
  ]);
  assertCertifiedResult(ownerReaderSplit("generated-optional-reads", generated), [
    [null, null],
    [null, null],
    [null, null],
    [0, "present"],
  ]);
}

if (runCase("optional-read-evaluated-receiver")) {
  // Rebinding the receiver during key evaluation leaves the reached getter's
  // original `this` intact. The absent receiver still runs its getter once.
  assertCertifiedResult(
    ownerReaderSplit(
      "optional-read-evaluated-receiver",
      `const run = () => {
      let trace = "";
      let current;
      const holder = { get field() { trace += "r"; return current; } };
      const key = () => { trace += "k"; current = null; return "selected"; };
      const skipped = holder.field?.[key()] === undefined;
      current = {
        value: 42,
        get selected() { trace += "g"; return this.value; }
      };
      const result = holder.field?.[key()];
      return [skipped, result, trace];
    };
    const result = run();
    export { result };`
    ),
    [true, 42, "rrkg"]
  );
}

if (runCase("generated-optional-evaluated-receiver")) {
  const fieldOf = (name) => ({
    type: "MemberExpression",
    object: id("record"),
    property: id(name),
    computed: false,
    optional: false,
  });
  const generatedReceiverReads = program([
    variable("const", "record", {
      type: "ObjectExpression",
      properties: [
        property("empty", literal(null)),
        property("filled", { type: "ObjectExpression", properties: [property("id", literal(7))] }),
      ],
    }),
    variable("const", "result", {
      type: "ArrayExpression",
      elements: ["empty", "filled"].map((name) => optionalMember(fieldOf(name), id("id"), false)),
    }),
    exportNamed([["result", "result"]]),
  ]);
  const generatedReads = ownerReaderSplit("generated-optional-evaluated-receiver", generatedReceiverReads);
  assertCertifiedResult(generatedReads, [null, 7]);
  assert.deepEqual(
    lower(generatedReads.files.sourceFile, generatedReads.files.specFile, generatedReads.out, ["--check-output"])
      .verdict,
    generatedReads.verdict
  );
}

if (runCase("optional-read-receiver-error")) {
  const failedReceiver = ownerReaderSplit(
    "optional-read-receiver-error",
    `const holder = { get field() { return null.field; } };
    const result = holder.field?.id;
    export { result };`
  );
  assertRejectedOrOrderKeptSlice(failedReceiver, /original threw during linked initialization/);
  assert.deepEqual(observe(failedReceiver.files.sourceFile, "result"), { error: "TypeError" });
}

if (runCase("optional-method-evaluated-receiver")) {
  // An evaluated receiver runs once per call. Only the reached call looks up a
  // method and computes the argument, in that order.
  const evaluated = ownerReaderSplit(
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
  assertCertifiedResult(evaluated, [true, 42, "ppga"]);
  assert.deepEqual(
    lower(evaluated.files.sourceFile, evaluated.files.specFile, evaluated.out, ["--check-output"]).verdict,
    evaluated.verdict
  );
}

if (runCase("optional-method-lexical-receiver")) {
  // Changing the lexical binding during argument evaluation leaves the original
  // receiver in the call. An undefined receiver skips that change altogether.
  assertCertifiedResult(
    ownerReaderSplit(
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
}

if (runCase("generated-optional-method-calls")) {
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
  assertCertifiedResult(ownerReaderSplit("generated-optional-method-calls", generatedCalls), [null, false]);
}

if (runCase("optional-method-falsy-receiver")) {
  const missingMethod = ownerReaderSplit(
    "optional-method-falsy-receiver",
    `const run = (target) => target?.method();
    const result = run(false);
    export { result };`
  );
  assertRejectedOrOrderKeptSlice(missingMethod, /original threw during linked initialization/);
  assert.deepEqual(observe(missingMethod.files.sourceFile, "result"), { error: "TypeError" });
}

if (runCase("optional-method-tdz")) {
  const uninitialized = ownerReaderSplit(
    "optional-method-tdz",
    `const run = () => {
    const result = receiver?.method();
    const receiver = { method() { return 7; } };
    return result;
  };
  const result = run();
  export { result };`
  );
  assertRejectedOrOrderKeptSlice(uninitialized, /original threw during linked initialization/);
  assert.deepEqual(observe(uninitialized.files.sourceFile, "result"), { error: "ReferenceError" });
}

if (runCase("read-before-initialization-order")) {
  const reordered = `function read() { return current?.value ?? "missing"; }
  var current;
  current = { value: "ready" };
  const result = read();
  export { result };`;
  const placements = { [owner]: ["#0"], [state]: ["#1", "#2"], [reader]: ["#3"], [entry]: ["#4"] };
  assertCertifiedResult(ownerReaderSplit("ready-before-read", reordered, placements, [owner, state, reader]), "ready");
  const earlyOrder = [owner, reader, state];
  const early = ownerReaderSplit("read-before-ready", reordered, placements, earlyOrder);
  assertRejected(early, /probe executions completed normally with different final states/);
  const preview = path.join(early.dir, "preview");
  forcePreview(early.files, preview);
  assert.deepEqual(moduleEvaluationOrder(preview, entry), [...earlyOrder, entry]);
  assert.deepEqual(observe(early.files.sourceFile, "result"), { result: "ready" });
  assert.deepEqual(observe(path.join(preview, entry), "result"), { result: "missing" });
}

for (const [name, body, reason, expected] of [
  ["parenthesized-method", "(target?.method)()", /unsupported optional chain as a call target/, 42],
  ["optional-receiver-and-callee", "target?.method?.()", /unsupported optional call/, 42],
  ["computed-optional-method", 'target?.["method"]()', /unsupported computed optional method call/, 42],
  ["continued-member", "target?.record.value", /unsupported optional chain/, 17],
  ["delete-chain", "delete target?.value", /delete/, true],
]) {
  if (!runCase(name)) continue;
  const test = ownerReaderSplit(
    name,
    `const run = (target) => ${body};
      const result = run({ value: 42, method() { return this.value; }, record: { value: 17 } });
      export { result };`
  );
  assert.deepEqual(observe(test.files.sourceFile, "result"), { result: expected });
  assertRejectedOrOrderKeptSlice(test, reason);
}
