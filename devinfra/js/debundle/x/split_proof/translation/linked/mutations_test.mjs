import assert from "node:assert/strict";
import {
  array,
  arrow,
  assertCertifiedResult,
  assertRejectedOrOrderKeptSlice,
  binary,
  block,
  call,
  caseShard,
  exportNamed,
  id,
  literal,
  member,
  observe,
  program,
  property,
  returns,
  sequentialSplit,
  throws,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const worker = "worker.mjs";
const includesCase = caseShard();

function assertRejectedObservation(test, reason, observation) {
  assertRejectedOrOrderKeptSlice(test, reason);
  assert.deepEqual(observe(test.files.sourceFile, "result"), observation);
}

const assign = (operator, left, right) => ({ type: "AssignmentExpression", operator, left, right });
const update = (operator, argument, prefix) => ({ type: "UpdateExpression", operator, argument, prefix });
const record = (key, value) => ({ type: "ObjectExpression", properties: [property(key, literal(value))] });
const run = (body) =>
  program([
    variable("const", "run", arrow([], block(body))),
    variable("const", "result", call(id("run"))),
    exportNamed([["result", "result"]]),
  ]);

function expect(actual, expected) {
  return {
    type: "IfStatement",
    test: binary("!==", actual, literal(expected)),
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
  ["&=", 0x100000001, 3, 1, false],
  ["|=", "0x10", "2", 18, true],
  ["^=", -3.9, true, -4, false],
  ["<<=", 1, 63, -0x80000000, true],
  [">>=", -3, 1, -2, false],
  [">>>=", "-1", 0, 0xffffffff, true],
]) {
  if (!includesCase(operator)) continue;
  const target = onMember ? member(id("box"), literal("value"), true) : id("value");
  const source = run([
    onMember ? variable("const", "box", record("value", initial)) : variable("let", "value", literal(initial)),
    variable("const", "assigned", assign(operator, target, literal(rhs))),
    expect(id("assigned"), expected),
    expect(target, expected),
    returns(target),
  ]);
  assertCertifiedResult(sequentialSplit(operator, source), expected);
}

for (const [operator, initial, selectsRhs, onMember] of [
  ["&&=", 0, false, false],
  ["&&=", "old", true, true],
  ["||=", "old", false, true],
  ["||=", "", true, false],
  ["??=", 0, false, false],
  ["??=", null, true, true],
]) {
  const name = `${operator}-${selectsRhs}`;
  if (!includesCase(name)) continue;
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
  assertCertifiedResult(sequentialSplit(name, source), expected);
}

for (const [operator, prefix, onMember, updated] of [
  ["++", true, true, 8],
  ["++", false, false, 8],
  ["--", true, false, 6],
  ["--", false, true, 6],
]) {
  const name = `${operator}-${prefix}`;
  if (!includesCase(name)) continue;
  const target = onMember ? member(id("box"), id("value")) : id("value");
  const expected = prefix ? updated : 7;
  const source = run([
    onMember ? variable("const", "box", record("value", "7")) : variable("let", "value", literal("7")),
    variable("const", "assigned", update(operator, target, prefix)),
    expect(id("assigned"), expected),
    expect(target, updated),
    returns(array([id("assigned"), target])),
  ]);
  assertCertifiedResult(sequentialSplit(name, source), [expected, updated]);
}

if (includesCase("reference-order")) {
  assertCertifiedResult(
    sequentialSplit(
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
}

if (includesCase("abrupt-read")) {
  assertCertifiedResult(
    sequentialSplit(
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
}

if (includesCase("delete-own-property")) {
  assertCertifiedResult(
    sequentialSplit(
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
}

// Updating an imported binding throws TypeError on the write; the catch returns the unchanged import.
for (const [name, mutation] of [
  ["arithmetic-import", "shared += 1"],
  ["update-import", "shared++"],
  ["short-circuited-import", 'shared ||= "updated"'],
]) {
  if (!includesCase(name)) continue;
  assertCertifiedResult(
    sequentialSplit(
      name,
      `import { shared } from "./provider.mjs";
      const run = () => { try { return ${mutation}; } catch { return shared; } };
      export const result = run();`,
      { [entry]: ["#0", "#2"], [worker]: ["#1"] },
      [worker],
      'export let shared = "original";'
    ),
    "original"
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
  if (!includesCase(name)) continue;
  const source = `const run = () => { ${mutation} }; const result = run(); export { result };`;
  assertRejectedObservation(sequentialSplit(name, source), reason, { result });
}
