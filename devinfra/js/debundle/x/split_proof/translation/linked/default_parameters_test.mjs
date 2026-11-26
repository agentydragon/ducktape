import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  arrow,
  assertCertifiedResult,
  assertRejectedOrOrderKeptSlice,
  block,
  call,
  caseShard,
  exportNamed,
  forcePreview,
  id,
  literal,
  observe,
  program,
  returns,
  sequentialSplit,
  throws,
  variable,
  workerSplit,
} from "./harness.mjs";

const entry = "entry.mjs";
const worker = "worker.mjs";
const includesCase = caseShard();

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
  if (!includesCase(name)) continue;
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
  assertCertifiedResult(sequentialSplit(name, source), expected);
}

if (includesCase("function-length-receiver-name")) {
  assertCertifiedResult(
    sequentialSplit(
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
}

// Defaults resolve outside the body's lexical scope. A closure created by a
// default still reads the same parameter cell the function body can update.
if (includesCase("parameter-environment")) {
  assertCertifiedResult(
    sequentialSplit(
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
}

for (const [name, signature] of [
  ["self-tdz", "value = value"],
  ["later-tdz", "value = typeof later, later = 1"],
]) {
  if (!includesCase(name)) continue;
  assertCertifiedResult(
    sequentialSplit(
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
  if (!includesCase(`reordered-default-${explicit}`)) continue;
  const source = `const label = "ready";
    const value = ((input = label) => input)(${explicit ? '"ready"' : ""});
    export { value as result };`;
  const order = [worker, "label.mjs"];
  const test = sequentialSplit(
    `reordered-default-${explicit}`,
    source,
    { "label.mjs": ["#0"], [worker]: ["#1"], [entry]: ["#2"] },
    order
  );
  if (explicit) {
    assertCertifiedResult(test, "ready");
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

for (const [name, expression, call, expected] of [
  ["default-destructuring", "({ x } = { x: offset }) => x", "pick()", 7],
  ["mixed-destructuring", "(x = offset, { y }) => x + y", "pick(undefined, { y: 3 })", 10],
]) {
  if (!includesCase(`parameter-${name}`)) continue;
  assertCertifiedResult(
    sequentialSplit(
      `reached-parameter-${name}`,
      `const offset = 7; const pick = ${expression}; export const result = [pick.length, ${call}];`
    ),
    [0, expected]
  );
}

for (const [name, expression, args, length, expected] of [
  ["nested-object", "({ x: { y = offset } }) => y", [{ x: {} }], 1, 7],
  ["array-pattern", "([x, y], z = offset) => x + y + z", [[1, 2]], 1, 10],
  ["rest-default-pattern", "(...[x = 3]) => x + 1", [4], 0, 5],
]) {
  if (!includesCase(`parameter-${name}`)) continue;
  const source = `const offset = 7; const pick = ${expression}; export { pick };`;
  const replay = sequentialSplit(`deferred-parameter-${name}`, source);
  assert.equal(replay.verdict.status, "certified", replay.log);
  for (const file of [replay.files.sourceFile, path.join(replay.out, entry)]) {
    const { pick } = await import(pathToFileURL(file).href);
    assert.equal(pick.length, length);
    assert.equal(pick(...args), expected);
  }
  assertRejectedOrOrderKeptSlice(
    sequentialSplit(
      `reached-parameter-${name}`,
      `const offset = 7; const pick = ${expression}; export const result = pick();`
    ),
    /deferred function pick:/
  );
}

if (includesCase("arguments-before-defaults")) {
  assertCertifiedResult(
    workerSplit(
      "arguments-before-defaults",
      `function pick(first = arguments[1], second = 3) { return [first, second, arguments.length]; }
       export const result = pick(undefined, 4);`
    ),
    [4, 4, 2]
  );
}

if (includesCase("redeclared-parameter")) {
  const { out, verdict, log } = sequentialSplit(
    "redeclared-parameter",
    "const pick = function (value = 1) { var value; return value; }; const result = true; export { result };"
  );
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, /body var overlaps default parameters/, log);
  assert(!existsSync(out));
}

if (includesCase("body-var-shadows-default")) {
  const { out, verdict, log } = sequentialSplit(
    "body-var-shadows-default",
    `const label = "outer";
    const pick = (value = () => label) => { var label = "body"; return value(); };
    export const result = pick();`
  );
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, /body var overlaps default parameters or their references/);
  assert(!existsSync(out));
}

// A BigInt literal keeps these bodies on the deferred-code path.
if (includesCase("deferred-body-defaults-and-methods")) {
  const retained = sequentialSplit(
    "deferred-body-defaults-and-methods",
    `const input = "123";
   function matches(x, text = input) { return text === (123n).toString() && x; }
   class Record {
     constructor(text = input) { this.value = text === (123n).toString(); }
     get matches() { return input === (123n).toString(); }
     read(text = input) { return text === (123n).toString(); }
   }
   export { matches, Record };`,
    { "input.mjs": ["#0"], [worker]: ["#1", "#2"], [entry]: ["#3"] },
    ["input.mjs", worker]
  );
  assert.equal(retained.verdict.status, "certified", retained.log);
  for (const file of [retained.files.sourceFile, path.join(retained.out, entry)]) {
    const { matches, Record } = await import(pathToFileURL(file).href);
    assert.equal(matches.length, 1);
    assert.equal(Record.length, 0);
    assert.equal(Record.prototype.read.length, 0);
    const record = new Record();
    assert.deepEqual([matches(true), record.value, record.matches, record.read("other")], [true, true, true, false]);
  }
}

for (const [name, declaration, invocation] of [
  ["defaults", "function read(x = 1) { return 1n; }", "read()"],
  ["getter", "const box = { get value() { return 1n; } };", "box.value"],
  ["constructor", "class Box { constructor() { this.value = 1n; } }", "new Box()"],
]) {
  if (!includesCase(`invoked-deferred-${name}`)) continue;
  assertRejectedOrOrderKeptSlice(
    workerSplit(`invoked-deferred-${name}`, `${declaration} export const result = ${invocation};`),
    /deferred function .*unsupported literal/
  );
}
