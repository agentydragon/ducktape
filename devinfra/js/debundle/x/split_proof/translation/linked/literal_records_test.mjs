import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  assertCertified,
  root,
  id,
  literal,
  call,
  returns,
  block,
  tryStatement,
  catchClause,
  arrow,
  variable,
  property,
  exportNamed,
  program,
  observe,
  lower,
  prepare,
  moduleEvaluationOrder,
  forcePreview,
  workerSplit,
  assertWorkerResult,
  observedProgram,
} from "./harness.mjs";

// Vary the number, spelling and values of static properties across a module boundary.
for (const seed of [0, 1, 2, 3, 14]) {
  const expected = {};
  const properties = Array.from({ length: seed }, (_, i) => {
    const key = i % 2 ? `quoted-key-${i}` : `field${i}`;
    expected[key] = i % 2 ? i : seed;
    const prop = property(key, i % 2 ? literal(i) : id("worker"));
    return i % 2 ? { ...prop, key: literal(key) } : prop;
  });
  assertWorkerResult(
    `generated-record-${seed}`,
    observedProgram(literal(seed), { type: "ObjectExpression", properties }),
    expected
  );
}

assertWorkerResult(
  "record-nested-values",
  `const worker = { source: ["first", { value: 42 }] };
  export const result = {
    nested: { field: worker.source[1].value },
    list: [worker.source[0], { key: worker.source.length }],
  };`,
  { nested: { field: 42 }, list: ["first", { key: 2 }] }
);

for (const [seed, key] of [id("value"), literal("quoted-key")].entries()) {
  const value = seed + 10;
  const provider = { type: "ObjectExpression", properties: [{ ...property("value", literal(value)), key }] };
  const read = {
    type: "MemberExpression",
    object: id("worker"),
    property: key,
    computed: key.type === "Literal",
    optional: false,
  };
  const list = { type: "ArrayExpression", elements: [read, { type: "ObjectExpression", properties: [] }] };
  const result = { type: "ObjectExpression", properties: [property("read", read), property("list", list)] };
  assertWorkerResult(`generated-record-values-${seed}`, observedProgram(provider, result), {
    read: value,
    list: [value, {}],
  });
}

// The first read throws a number. Reading the second value out of order
// throws a ReferenceError, which must not reach the catch instead.
assertWorkerResult(
  "record-value-order",
  `const worker = () => {
    const key = () => { throw 7; };
    try {
      return { first: ({})[key()], second: later.field };
      let later;
    } catch (error) {
      return typeof error;
    }
  };
  export const result = worker() === "number" ? "number" : (() => { throw 0; })();`,
  "number"
);

// The first property must call with its receiver and its throw must skip the second property.
assertWorkerResult(
  "record-call-receiver-and-order",
  `const worker = () => {
    const read = function () { throw this.value; };
    const receiver = { value: "first", read };
    try {
      return { first: receiver.read(), second: (() => { throw "second"; })() };
    } catch (error) {
      return error;
    }
  };
  export const result = worker() === "first" ? "first" : (() => { throw 0; })();`,
  "first"
);

// The same call returns the fallback before `later` initializes and reads the live binding after it initializes.
{
  const read = arrow(
    [id("fallback")],
    block([tryStatement(block([returns(id("later"))]), catchClause(null, block([returns(id("fallback"))])))])
  );
  const source = program([
    variable("const", "read", read),
    variable("const", "record", {
      type: "ObjectExpression",
      properties: [property("value", call(id("read"), [literal("before")]))],
    }),
    variable("const", "later", literal("after")),
    exportNamed([["record", "result"]]),
  ]);
  for (const reordered of [false, true]) {
    const dir = path.join(root, `generated-record-call-reordered-${reordered}`);
    const spec = {
      entry: "entry.mjs",
      modules: { "record.mjs": ["#0", "#1"], "later.mjs": ["#2"], "entry.mjs": ["#3"] },
      order: reordered ? ["later.mjs", "record.mjs"] : ["record.mjs", "later.mjs"],
      sequential: true,
    };
    const files = prepare(dir, { source, provider: "", spec });
    const out = path.join(dir, "out");
    const { verdict, log } = lower(files.sourceFile, files.specFile, out);
    let candidate = out;
    if (reordered) {
      assert.equal(verdict.status, "rejected", log);
      assert.match(verdict.reason, /probe executions completed normally with different final states/, log);
      assert(!existsSync(out));
      candidate = path.join(dir, "preview");
      forcePreview(files, candidate);
    } else {
      assertCertified(verdict, log);
    }
    assert.deepEqual(moduleEvaluationOrder(candidate, spec.entry), [...spec.order, spec.entry], log);
    assert.deepEqual(observe(files.sourceFile, "result"), { result: { value: "before" } }, log);
    assert.deepEqual(observe(path.join(candidate, spec.entry), "result"), {
      result: { value: reordered ? "after" : "before" },
    });
  }
}

// A wide, straight-line initializer consumes one level of fuel per property.
const fields = Array.from({ length: 65 }, (_, i) => property(`field${i}`, literal(i)));
assertWorkerResult(
  "wide-record",
  observedProgram(literal(0), { type: "ObjectExpression", properties: fields }),
  Object.fromEntries(fields.map((field, i) => [field.key.name, i]))
);

// After retrying the wide record, report the actual TDZ on its final property.
{
  const value = { type: "ObjectExpression", properties: [...fields, property("tail", id("result"))] };
  const { verdict, out } = workerSplit("wide-record-tdz", observedProgram(literal(0), value));
  assert.equal(verdict.status, "rejected");
  assert.match(verdict.reason, /original threw during linked initialization/);
  assert(!existsSync(out));
}

assertWorkerResult(
  "record-optional-member",
  "const worker = { field: 1 }; export const result = { key: worker?.field };",
  { key: 1 }
);
assertWorkerResult("record-spread", "const worker = () => ({ ...worker }); export const result = worker();", {});
assertWorkerResult("record-computed", 'const worker = () => ({ ["key"]: 1 }); export const result = worker();', {
  key: 1,
});
assertWorkerResult(
  "computed-getter",
  `const worker = () => ({ get ["key"]() { return 1; } }); export const result = worker();`,
  { key: 1 }
);
assertWorkerResult("record-duplicate", `const worker = () => ({ key: 1, "key": 2 }); export const result = worker();`, {
  key: 2,
});
{
  const { verdict, out } = workerSplit(
    "record-array-hole",
    "const worker = () => ({ nested: { key: [, 1] } }); export const result = worker();"
  );
  assert.equal(verdict.status, "rejected");
  assert.match(verdict.reason, /unsupported array hole/);
  assert(!existsSync(out));
}
