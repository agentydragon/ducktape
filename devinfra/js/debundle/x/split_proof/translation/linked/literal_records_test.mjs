import assert from "node:assert/strict";
import path from "node:path";
import { withSplitPreview } from "../testing/split_preview.mjs";
import { writeDeployment } from "../testing/deployment_manifest.mjs";
import { assertPublicEmissionRefused, assertPublicRecheckRefused } from "../testing/public_refusal.mjs";
import { probeLinkedModel } from "../testing/linked_model_probe.mjs";
import {
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
  prepare,
  moduleEvaluationOrder,
  observedProgram,
} from "./harness.mjs";

// Inspect primitive leaves of the observed result in the model as well as Node.
function observedLeaves(value, prefix = ["result"]) {
  if (value !== null && typeof value === "object") {
    return Object.entries(value).flatMap(([key, child]) => observedLeaves(child, [...prefix, key]));
  }
  if (value === null) return []; // Node's JSON view also turns array holes into null.
  let text;
  if (typeof value === "number") {
    const bits = Buffer.alloc(8);
    bits.writeDoubleBE(value);
    text = `#${bits.readBigUInt64BE()}`;
  } else text = JSON.stringify(value);
  return [{ path: prefix, original: text, split: text }];
}

async function assertRecordWorkerResult(name, source, expected, entryUnits = ["#1"]) {
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source,
    provider: "",
    spec: { entry: "entry.mjs", modules: { "worker.mjs": ["#0"], "entry.mjs": entryUnits }, order: ["worker.mjs"] },
  });
  const out = path.join(dir, "out");
  const manifest = writeDeployment(files.sourceFile, files.specFile, out);
  assertPublicEmissionRefused(manifest, out);
  await withSplitPreview({ source: files.sourceFile, spec: files.specFile }, async ({ directory, entryFile }) => {
    if (name === "wide-record") {
      assertPublicEmissionRefused(manifest, directory);
      assertPublicRecheckRefused(manifest, directory);
    }
    // The model probe reads data properties only; Node exercises the getter.
    const expectedLeaves = name === "computed-getter" ? [] : observedLeaves(expected);
    const model = probeLinkedModel({
      source: files.sourceFile,
      spec: files.specFile,
      directory,
      observe: expectedLeaves.map(({ path }) => path),
    });
    assert.equal(model.status, "matched", `${name}: ${JSON.stringify(model)}`);
    assert(model.fuel > 0, name);
    if (name === "wide-record") assert.equal(model.fuel, 64 + 65);
    assert.deepEqual(model.assumptions, [], name);
    assert.deepEqual(model.host_refs, [], name);
    assert.deepEqual(model.observed, expectedLeaves, name);
    for (const file of [files.sourceFile, entryFile])
      assert.deepEqual(observe(file, "result"), { result: expected }, name);
  });
}

// Vary the number, spelling and values of static properties across a module boundary.
for (const seed of [0, 1, 2, 3, 14]) {
  const expected = {};
  const properties = Array.from({ length: seed }, (_, i) => {
    const key = i % 2 ? `quoted-key-${i}` : `field${i}`;
    expected[key] = i % 2 ? i : seed;
    const prop = property(key, i % 2 ? literal(i) : id("worker"));
    return i % 2 ? { ...prop, key: literal(key) } : prop;
  });
  await assertRecordWorkerResult(
    `generated-record-${seed}`,
    observedProgram(literal(seed), { type: "ObjectExpression", properties }),
    expected
  );
}

await assertRecordWorkerResult(
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
  await assertRecordWorkerResult(`generated-record-values-${seed}`, observedProgram(provider, result), {
    read: value,
    list: [value, {}],
  });
}

// The first read throws a number. Reading the second value out of order
// throws a ReferenceError, which must not reach the catch instead.
await assertRecordWorkerResult(
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
await assertRecordWorkerResult(
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
    const manifest = writeDeployment(files.sourceFile, files.specFile, out);
    assertPublicEmissionRefused(manifest, out);
    await withSplitPreview(
      { source: files.sourceFile, spec: files.specFile, expectedExit: reordered ? 1 : 0 },
      async ({ directory, entryFile }) => {
        assert.deepEqual(moduleEvaluationOrder(directory, spec.entry), [...spec.order, spec.entry]);
        const model = probeLinkedModel({
          source: files.sourceFile,
          spec: files.specFile,
          directory,
          observe: [["result", "value"]],
        });
        if (reordered) {
          assert.equal(model.status, "refused");
          assert.equal(model.reason_code, "probe_mismatch");
          assert.deepEqual(model.observation, {
            kind: "value",
            path: ["result", "value"],
            original: '"before"',
            split: '"after"',
          });
        } else {
          assert.equal(model.status, "matched");
          assert(model.fuel > 0);
          assert.deepEqual(model.assumptions, []);
          assert.deepEqual(model.host_refs, []);
          assert.deepEqual(model.observed, [{ path: ["result", "value"], original: '"before"', split: '"before"' }]);
        }
        assert.deepEqual(observe(files.sourceFile, "result"), { result: { value: "before" } });
        assert.deepEqual(observe(entryFile, "result"), { result: { value: reordered ? "after" : "before" } });
      }
    );
  }
}

// A wide, straight-line initializer consumes one level of fuel per property.
const fields = Array.from({ length: 65 }, (_, i) => property(`field${i}`, literal(i)));
await assertRecordWorkerResult(
  "wide-record",
  observedProgram(literal(0), { type: "ObjectExpression", properties: fields }),
  Object.fromEntries(fields.map((field, i) => [field.key.name, i]))
);

// After retrying the wide record, report the actual TDZ on its final property.
{
  const value = { type: "ObjectExpression", properties: [...fields, property("tail", id("result"))] };
  const source = observedProgram(literal(0), value);
  const dir = path.join(root, "wide-record-tdz");
  const spec = { entry: "entry.mjs", modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1"] }, order: ["worker.mjs"] };
  const files = prepare(dir, { source, provider: "", spec });
  const out = path.join(dir, "out");
  const manifest = writeDeployment(files.sourceFile, files.specFile, out);
  assertPublicEmissionRefused(manifest, out);
  await withSplitPreview({ source: files.sourceFile, spec: files.specFile }, async ({ directory, entryFile }) => {
    const model = probeLinkedModel({ source: files.sourceFile, spec: files.specFile, directory });
    assert.equal(model.status, "refused");
    assert.equal(model.reason_code, "probe_throw");
    assert.match(model.reason, /original threw during linked initialization/);
    assert.deepEqual(observe(files.sourceFile, "result"), { error: "ReferenceError" });
    assert.deepEqual(observe(entryFile, "result"), { error: "ReferenceError" });
  });
}

await assertRecordWorkerResult(
  "record-optional-member",
  "const worker = { field: 1 }; export const result = { key: worker?.field };",
  { key: 1 }
);
await assertRecordWorkerResult(
  "record-spread",
  "const worker = () => ({ ...worker }); export const result = worker();",
  {}
);
await assertRecordWorkerResult(
  "record-computed",
  'const worker = () => ({ ["key"]: 1 }); export const result = worker();',
  {
    key: 1,
  }
);
await assertRecordWorkerResult(
  "computed-getter",
  `const worker = () => ({ get ["key"]() { return 1; } }); export const result = worker();`,
  { key: 1 }
);
await assertRecordWorkerResult(
  "record-duplicate",
  `const worker = () => ({ key: 1, "key": 2 }); export const result = worker();`,
  {
    key: 2,
  }
);
await assertRecordWorkerResult(
  "record-array-hole",
  "const worker = () => ({ nested: { key: [, 1] } }); export const result = worker();",
  { nested: { key: [null, 1] } }
);
