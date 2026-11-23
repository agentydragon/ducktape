import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  arrow,
  block,
  call,
  exportNamed,
  forcePreview,
  id,
  literal,
  lower,
  observe,
  prepare,
  program,
  returns,
  root,
  throws,
  variable,
} from "./linked_programs_test_support.mjs";

const entry = "entry.mjs";
const worker = "worker.mjs";

function split(name, source, modules = { [worker]: ["#0"], [entry]: ["#1", "#2"] }, order = [worker]) {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider: "", spec: { entry, modules, order, sequential: true } });
  const out = path.join(dir, "out");
  return { files, out, dir, ...lower(files.sourceFile, files.specFile, out) };
}

function assertCertified({ files, out, verdict, log }, expected) {
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", log);
  assert.deepEqual(observe(files.sourceFile, "result"), { result: expected });
  assert.deepEqual(observe(path.join(out, entry), "result"), { result: expected });
}

function defaultParam(name, value) {
  return { type: "AssignmentPattern", left: id(name), right: value };
}

// The guard runs during initialization and rejects a consistently wrong lowering.
for (const [name, args, expected] of [
  ["missing", [], "default"],
  ["undefined", [id("undefined")], "default"],
  ["null", [literal(null)], null],
  ["false", [literal(false)], false],
]) {
  const workerDecl = variable(
    "const",
    "pick",
    arrow([defaultParam("first", literal("default")), defaultParam("second", id("first"))], id("second"))
  );
  const check = arrow(
    [],
    block([
      variable("const", "value", call(id("pick"), args)),
      {
        type: "IfStatement",
        test: { type: "BinaryExpression", operator: "!==", left: id("value"), right: literal(expected) },
        consequent: throws(literal("wrong parameter value")),
        alternate: null,
      },
      returns(id("value")),
    ])
  );
  const source = program([workerDecl, variable("const", "result", call(check)), exportNamed([["result", "result"]])]);
  assertCertified(split(name, source), expected);
}

assertCertified(
  split(
    "function-length-receiver-name",
    `function pick(first, value = this.value, last, fn = () => value) {
      if (pick.length !== 1 || fn.name !== "fn" || fn() !== "receiver") throw "wrong default metadata";
      return [first, value, last];
    }
    const box = { value: "receiver", pick };
    export const result = box.pick("first", undefined, "last");`
  ),
  ["first", "receiver", "last"]
);

// Defaults resolve outside the body's lexical scope. A closure created by a
// default still reads the same parameter cell the function body can update.
assertCertified(
  split(
    "parameter-environment",
    `const label = "outer";
    function run(value = label, capture = () => value, readLabel = () => label) {
      const before = value;
      value = "changed";
      var description = "body";
      const label = description;
      if (before !== "outer" || capture() !== "changed" || readLabel() !== "outer") throw "wrong parameter scope";
      return [value, label];
    }
    export const result = run();`,
    { "value.mjs": ["#0"], [worker]: ["#1"], [entry]: ["#2"] },
    ["value.mjs", worker]
  ),
  ["changed", "body"]
);

for (const [name, signature] of [
  ["self-tdz", "value = value"],
  ["later-tdz", "value = typeof later, later = 1"],
]) {
  assertCertified(
    split(
      name,
      `const pick = (${signature}) => value;
      const result = (() => {
        try { pick(); } catch (error) { if (typeof error === "object") return "tdz"; throw error; }
        throw "missing parameter TDZ";
      })();
      export { result };`
    ),
    "tdz"
  );
}

// Moving the eager call before its provider changes the default's read. Supplying
// an explicit argument skips the same default and allows the new evaluation order.
for (const explicit of [false, true]) {
  const source = `const label = "ready";
    const value = ((input = label) => input)(${explicit ? '"ready"' : ""});
    export { value as result };`;
  const order = [worker, "label.mjs"];
  const test = split(
    `reordered-default-${explicit}`,
    source,
    { "label.mjs": ["#0"], [worker]: ["#1"], [entry]: ["#2"] },
    order
  );
  if (explicit) {
    assertCertified(test, "ready");
  } else {
    assert.equal(test.verdict.status, "rejected", test.log);
    assert.match(test.verdict.reason, /split threw during linked initialization/);
    assert(!existsSync(test.out));
    assert.deepEqual(observe(test.files.sourceFile, "result"), { result: "ready" });
    const preview = path.join(test.dir, "preview");
    forcePreview(test.files, preview);
    assert.deepEqual(observe(path.join(preview, entry), "result"), { error: "ReferenceError" });
  }
}

for (const [name, expression, reason] of [
  ["default-destructuring", "({ value } = { value: 1 }) => value", /expected an identifier in a default parameter/],
  ["mixed-destructuring", "(x = 1, { value }) => value", /unsupported object pattern alongside default parameters/],
  ["rest", "(value = 1, ...rest) => value", /unsupported function parameter RestElement/],
  [
    "redeclared-parameter",
    "function (value = 1) { var value; return value; }",
    /body var overlaps named default parameters/,
  ],
  [
    "implicit-arguments",
    "function (value = arguments[0]) { return value; }",
    /unsupported (?:implicit arguments reference|global arguments)/,
  ],
]) {
  const { out, verdict, log } = split(name, `const pick = ${expression}; const result = true; export { result };`);
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, reason, log);
  assert(!existsSync(out));
}

{
  const { out, verdict, log } = split(
    "body-var-shadows-default",
    `const label = "outer";
    const pick = (value = () => label) => { var label = "body"; return value(); };
    export const result = pick();`
  );
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, /body var overlaps named default parameters or their references/);
  assert(!existsSync(out));
}
