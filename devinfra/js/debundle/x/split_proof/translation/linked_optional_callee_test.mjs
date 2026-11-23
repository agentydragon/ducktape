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
} from "./linked_programs_test_support.mjs";

const entry = "entry.mjs";
const owner = "owner.mjs";
const reader = "reader.mjs";
const writer = "writer.mjs";

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

const evaluated = split(
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
assertCertified(evaluated, ["original!", "rgac"]);
assert.deepEqual(
  lower(evaluated.files.sourceFile, evaluated.files.specFile, evaluated.out, ["--check-output"]).verdict,
  evaluated.verdict
);

assertCertified(
  split(
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

for (const [name, receiver, caught, expected] of [
  ["throwing-getter", '{ get method() { trace += "g"; throw "getter"; } }', 'error === "getter"', "g"],
  ["noncallable-method", "{ method: false }", 'typeof error === "object"', "a"],
  ["null-receiver", "null", 'typeof error === "object"', ""],
]) {
  assertCertified(
    split(
      name,
      `const run = () => {
        let trace = "";
        const argument = () => { trace += "a"; };
        try {
          (${receiver}).method?.(argument());
        } catch (error) {
          if (${caught} && trace === ${JSON.stringify(expected)}) return trace;
          throw error;
        }
        throw "expected an abrupt call";
      };
      const result = run();
      export { result };`
    ),
    expected
  );
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
  variable("const", "result", {
    type: "ArrayExpression",
    elements: [
      object([property("method", literal(null))]),
      object([]),
      object([
        property("value", literal(42)),
        {
          ...property("method", functionExpression([], block([returns(member({ type: "ThisExpression" }, "value"))]))),
          method: true,
        },
      ]),
    ].map((receiver) => ({
      type: "LogicalExpression",
      operator: "??",
      left: call(id("invoke"), [receiver]),
      right: literal("skipped"),
    })),
  }),
  exportNamed([["result", "result"]]),
]);
assertCertified(split("generated-optional-callee", generated), ["skipped", "skipped", 42]);

const reordered = `const target = { value: "ready" };
  target.method = function () { return this.value; };
  const result = target.method?.() ?? "missing";
  export { result };`;
const placements = { [owner]: ["#0"], [writer]: ["#1"], [reader]: ["#2"], [entry]: ["#3"] };
assertCertified(split("install-before-call", reordered, placements, [owner, writer, reader]), "ready");
const earlyOrder = [owner, reader, writer];
const early = split("call-before-install", reordered, placements, earlyOrder);
assertRejected(early, /probe executions completed normally with different final states/);
const preview = path.join(early.dir, "preview");
forcePreview(early.files, preview);
assert.deepEqual(moduleEvaluationOrder(preview, entry), [...earlyOrder, entry]);
assert.deepEqual(observe(early.files.sourceFile, "result"), { result: "ready" });
assert.deepEqual(observe(path.join(preview, entry), "result"), { result: "missing" });

for (const [name, body, reason, expected] of [
  ["bare-callee", "const invoke = () => 42; return invoke?.();", /unsupported optional call/, 42],
  ["computed-callee", 'return target["method"]?.();', /unsupported computed optional method call/, { value: 42 }],
  ["continued-callee", "return target.method?.().value;", /unsupported optional chain/, 42],
]) {
  const test = split(
    name,
    `const run = (target) => { ${body} };
      const result = run({ method() { return { value: 42 }; } });
      export { result };`
  );
  assert.deepEqual(observe(test.files.sourceFile, "result"), { result: expected });
  assertRejected(test, reason);
}

const host = split(
  "optional-callee-opaque-host-value",
  `const run = () => ({}).toString?.();
   const result = run();
   export { result };`
);
assert.deepEqual(observe(host.files.sourceFile, "result"), { result: "[object Object]" });
assertRejected(host, /original reached an unknown result during linked initialization: host value .* is inspected/);
