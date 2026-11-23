import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  arrow,
  block,
  call,
  exportNamed,
  id,
  literal,
  lower,
  observe,
  prepare,
  program,
  property,
  returns,
  root,
  throws,
  variable,
} from "./linked_programs_test_support.mjs";

const entry = "entry.mjs";
const worker = "worker.mjs";

function split(name, source, modules = { [worker]: ["#0"], [entry]: ["#1", "#2"] }, provider = "") {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider, spec: { entry, modules, order: [worker], sequential: true } });
  const out = path.join(dir, "out");
  return { files, out, ...lower(files.sourceFile, files.specFile, out) };
}

function assertCertified({ files, out, verdict, log }, result) {
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", log);
  assert.deepEqual(observe(files.sourceFile, "result"), { result });
  assert.deepEqual(observe(path.join(out, entry), "result"), { result });
}

function assertRejected({ files, out, verdict, log }, reason, observation) {
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, reason, log);
  assert(!existsSync(out));
  assert.deepEqual(observe(files.sourceFile, "result"), observation);
}

const assign = (operator, left, right) => ({ type: "AssignmentExpression", operator, left, right });
const update = (operator, argument, prefix) => ({ type: "UpdateExpression", operator, argument, prefix });
const member = (object, key, computed = false) => ({ type: "MemberExpression", object, property: key, computed });
const record = (key, value) => ({ type: "ObjectExpression", properties: [property(key, literal(value))] });
const array = (...elements) => ({ type: "ArrayExpression", elements });
const run = (body) =>
  program([
    variable("const", "run", arrow([], block(body))),
    variable("const", "result", call(id("run"))),
    exportNamed([["result", "result"]]),
  ]);

function expect(actual, expected) {
  return {
    type: "IfStatement",
    test: { type: "BinaryExpression", operator: "!==", left: actual, right: literal(expected) },
    consequent: throws(literal("wrong mutation result")),
    alternate: null,
  };
}

// Assert values inside initialization so identical mislowering of both graphs still fails.
for (const [operator, initial, rhs, expected, onMember] of [
  ["+=", "item", 2, "item2", false],
  ["-=", "5", 2, 3, true],
  ["*=", 3, 4, 12, false],
  ["/=", 10, "2", 5, true],
  ["%=", 8, 3, 2, false],
]) {
  const target = onMember ? member(id("box"), literal("value"), true) : id("value");
  const source = run([
    onMember ? variable("const", "box", record("value", initial)) : variable("let", "value", literal(initial)),
    variable("const", "assigned", assign(operator, target, literal(rhs))),
    expect(id("assigned"), expected),
    expect(target, expected),
    returns(target),
  ]);
  assertCertified(split(operator, source), expected);
}

for (const [operator, initial, selectsRhs, onMember] of [
  ["&&=", 0, false, false],
  ["&&=", "old", true, true],
  ["||=", "old", false, true],
  ["||=", "", true, false],
  ["??=", 0, false, false],
  ["??=", null, true, true],
]) {
  const target = onMember ? member(id("box"), id("value")) : id("value");
  const rhs = selectsRhs ? arrow([], literal(42)) : call(arrow([], block([throws(literal("unexpected RHS"))])));
  const value = assign(operator, target, rhs);
  const expected = selectsRhs ? (onMember ? "" : "value") : initial;
  const readResult = selectsRhs ? member(id("assigned"), id("name")) : id("assigned");
  const source = run([
    onMember
      ? variable("const", "box", record("value", initial))
      : variable(selectsRhs ? "let" : "const", "value", literal(initial)),
    variable("const", "assigned", value),
    expect(readResult, expected),
    expect(selectsRhs ? call(target) : target, selectsRhs ? 42 : initial),
    returns(readResult),
  ]);
  assertCertified(split(`${operator}-${selectsRhs}`, source), expected);
}

for (const [operator, prefix, onMember, updated] of [
  ["++", true, true, 8],
  ["++", false, false, 8],
  ["--", true, false, 6],
  ["--", false, true, 6],
]) {
  const target = onMember ? member(id("box"), id("value")) : id("value");
  const expected = prefix ? updated : 7;
  const source = run([
    onMember ? variable("const", "box", record("value", "7")) : variable("let", "value", literal("7")),
    variable("const", "assigned", update(operator, target, prefix)),
    expect(id("assigned"), expected),
    expect(target, updated),
    returns(array(id("assigned"), target)),
  ]);
  assertCertified(split(`${operator}-${prefix}`, source), [expected, updated]);
}

assertCertified(
  split(
    "reference-order",
    `const run = () => {
      let phase = "start";
      const box = { value: 2 };
      const receiver = () => {
        if (phase !== "start") throw "unexpected receiver";
        phase = "receiver";
        return box;
      };
      const key = () => {
        if (phase !== "receiver") throw "unexpected key";
        phase = "key";
        return "value";
      };
      const rhs = () => {
        if (phase !== "key") throw "unexpected RHS";
        box.value = 50;
        return 3;
      };
      if ((receiver()[key()] *= rhs()) !== 6 || box.value !== 6) throw "wrong write";
      phase = "start";
      if (receiver()[key()]-- !== 6 || box.value !== 5) throw "wrong update";
      phase = "start";
      if (!delete receiver()[key()] || phase !== "key" || "value" in box) throw "wrong deletion";
      return phase;
    };
    const result = run(); export { result };`
  ),
  "key"
);

assertCertified(
  split(
    "abrupt-read",
    `const run = () => {
      try { target += (() => { throw "RHS ran"; })(); let target = 1; }
      catch (error) { if (typeof error === "object") return "TDZ"; throw error; }
      throw "missing abrupt completion";
    };
    const result = run(); export { result };`
  ),
  "TDZ"
);

assertCertified(
  split(
    "delete-own-property",
    `const run = () => {
      const list = [5];
      const record = {};
      if (!delete list[0] || 0 in list || list.length !== 1) throw "wrong array deletion";
      if (!delete record.toString || !("toString" in record)) throw "wrong inherited deletion";
      try { delete list.length; } catch { return list.length; }
      throw "deleted a nonconfigurable property";
    };
    const result = run(); export { result };`
  ),
  1
);

for (const [name, mutation] of [
  ["arithmetic-import", "shared += 1"],
  ["update-import", "shared++"],
  ["short-circuited-import", 'shared ||= "updated"'],
]) {
  assertRejected(
    split(
      name,
      `import { shared } from "./provider.mjs";
      const run = () => { try { return ${mutation}; } catch { return shared; } };
      export const result = run();`,
      { [entry]: ["#0", "#2"], [worker]: ["#1"] },
      'export let shared = "original";'
    ),
    /unsupported linked assignment to imported binding shared/,
    { result: "original" }
  );
}

for (const [name, mutation, reason, result] of [
  ["object-update", "let value = { valueOf: () => 3 }; return ++value;", /object is converted to a primitive/, 4],
  [
    "object-delete-key",
    'const value = { x: 1 }; return delete value[{ toString: () => "x" }];',
    /object is converted to a primitive/,
    true,
  ],
  ["nonreference-delete", "return delete (1 + 2);", /expected a member expression after delete/, true],
]) {
  const source = `const run = () => { ${mutation} }; const result = run(); export { result };`;
  assertRejected(split(name, source), reason, { result });
}
