import assert from "node:assert/strict";
import path from "node:path";
import {
  arrow,
  assertCertifiedResult,
  assertRejected,
  assertRejectedOrOrderKeptSlice,
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
  ownerReaderSplit,
  program,
  property,
  returns,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const owner = "owner.mjs";
const reader = "reader.mjs";
const writer = "writer.mjs";

const evaluated = ownerReaderSplit(
  "optional-callee-captures-receiver",
  `const run = () => {
    let trace = "";
    let target;
    const receiver = () => { trace += "r"; return target; };
    const argument = () => {
      trace += "a";
      target = { value: "new" };
      return "!";
    };
    target = {
      value: "original",
      get method() {
        trace += "g";
        return function (suffix) { trace += "c"; return this.value + suffix; };
      }
    };
    const result = receiver().method?.(argument());
    if (result !== "original!" || trace !== "rgac" || target.value !== "new") throw "incorrect call";
    return [result, trace];
  };
  const result = run();
  export { result };`
);
assertCertifiedResult(evaluated, ["original!", "rgac"]);
assert.deepEqual(
  lower(evaluated.files.sourceFile, evaluated.files.specFile, evaluated.out, ["--check-output"]).verdict,
  evaluated.verdict
);

assertCertifiedResult(
  ownerReaderSplit(
    "optional-callee-nullish-method",
    `const run = () => {
      let trace = "";
      const argument = () => { trace += "a"; };
      const record = { get method() { trace += "g"; return null; } };
      if (record.method?.(argument()) !== undefined || ({}).method?.(argument()) !== undefined) throw "nullish method";
      if (trace !== "g") throw "argument order";
      return trace;
    };
    const result = run();
    export { result };`
  ),
  "g"
);

for (const [name, expression, expectedError, thrown] of [
  ["throwing-getter", '({ get method() { throw "getter"; } }).method?.(fail())', 'error === "getter"', "getter"],
  ["argument-before-callability", "({ method: false }).method?.(fail())", 'error === "argument"', "argument"],
  ["noncallable-method", "({ method: false }).method?.()", 'typeof error === "object"', "TypeError"],
  ["null-receiver", "(null).method?.(fail())", 'typeof error === "object"', "TypeError"],
]) {
  // Only the expected error escapes. A different error or an absent throw reaches normal completion.
  const test = ownerReaderSplit(
    name,
    `const run = () => {
      const fail = () => { throw "argument"; };
      try { return ${expression}; }
      catch (error) {
        if (${expectedError}) throw typeof error === "string" ? { name: error } : error;
      }
    };
    const result = run();
    export { result };`
  );
  assert.deepEqual(observe(test.files.sourceFile, "result"), { error: thrown });
  assertRejectedOrOrderKeptSlice(test, /original threw during linked initialization/);
}

function member(object, name) {
  return { type: "MemberExpression", object, property: id(name), computed: false, optional: false };
}

function object(properties) {
  return { type: "ObjectExpression", properties };
}

const generated = program([
  variable(
    "const",
    "invoke",
    arrow([id("receiver")], {
      type: "ChainExpression",
      expression: { type: "CallExpression", callee: member(id("receiver"), "method"), arguments: [], optional: true },
    })
  ),
  variable(
    "const",
    "result",
    call(id("invoke"), [
      object([
        property("value", literal(42)),
        {
          ...property("method", functionExpression([], block([returns(member({ type: "ThisExpression" }, "value"))]))),
          method: true,
        },
      ]),
    ])
  ),
  exportNamed([["result", "result"]]),
]);
assertCertifiedResult(ownerReaderSplit("generated-optional-callee", generated), 42);

const reordered = `const target = { value: "ready" };
  target.method = function () { return this.value; };
  const result = target.method?.() ?? "missing";
  export { result };`;
const placements = { [owner]: ["#0"], [writer]: ["#1"], [reader]: ["#2"], [entry]: ["#3"] };
assertCertifiedResult(ownerReaderSplit("install-before-call", reordered, placements, [owner, writer, reader]), "ready");
const earlyOrder = [owner, reader, writer];
const early = ownerReaderSplit("call-before-install", reordered, placements, earlyOrder);
assertRejected(early, /probe executions completed normally with different final states/);
const preview = path.join(early.dir, "preview");
forcePreview(early.files, preview);
assert.deepEqual(moduleEvaluationOrder(preview, entry), [...earlyOrder, entry]);
assert.deepEqual(observe(early.files.sourceFile, "result"), { result: "ready" });
assert.deepEqual(observe(path.join(preview, entry), "result"), { result: "missing" });

for (const [name, body, reason, expected] of [
  ["bare-callee", "const invoke = () => 42; return invoke?.();", /unsupported optional call/, 42],
  ["computed-callee", 'return target["method"]?.();', /unsupported computed optional method call/, { value: 42 }],
]) {
  const test = ownerReaderSplit(
    name,
    `const run = (target) => { ${body} };
      const result = run({ method() { return { value: 42 }; } });
      export { result };`
  );
  assert.deepEqual(observe(test.files.sourceFile, "result"), { result: expected });
  assertRejectedOrOrderKeptSlice(test, reason);
}

const continued = ownerReaderSplit(
  "optional-callee-continuation",
  `const run = (present) => {
    let trace = "";
    const arg = () => { trace += "a"; return "!"; };
    const key = () => { trace += "k"; return "value"; };
    const target = {
      name: "receiver",
      get method() {
        trace += "g";
        return present ? function (value) {
          trace += "c";
          return { value: this.name + value };
        } : undefined;
      },
    };
    const value = target.method?.(arg())[key()];
    return [value, trace];
  };
  const result = [run(true), run(false)];
  export { result };`
);
assertCertifiedResult(continued, [
  ["receiver!", "gack"],
  [null, "g"],
]);
assert.deepEqual(
  lower(continued.files.sourceFile, continued.files.specFile, continued.out, ["--check-output"]).verdict,
  continued.verdict
);

const returnedUndefined = ownerReaderSplit(
  "optional-callee-continuation-undefined",
  `const run = (target) => target.method?.().value;
   const result = run({ method() { return undefined; } });
   export { result };`
);
assertRejectedOrOrderKeptSlice(returnedUndefined, /original threw during linked initialization/);
assert.deepEqual(observe(returnedUndefined.files.sourceFile, "result"), { error: "TypeError" });

const host = ownerReaderSplit(
  "optional-callee-opaque-host-value",
  `const run = () => ({}).toString?.();
   const result = run();
   export { result };`
);
assert.deepEqual(observe(host.files.sourceFile, "result"), { result: "[object Object]" });
assertRejectedOrOrderKeptSlice(
  host,
  /original reached an unknown result during linked initialization: host value .* is inspected/
);
