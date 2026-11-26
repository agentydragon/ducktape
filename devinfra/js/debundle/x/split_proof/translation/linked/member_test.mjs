import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  assertCertified,
  assertRejectedOrOrderKeptSlice,
  array,
  arrow,
  binary,
  block,
  call,
  conditional,
  forcePreview,
  functionExpression,
  id,
  literal,
  lower,
  member,
  prepare,
  program,
  property,
  returns,
  root,
  throws,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const load = (file) => import(pathToFileURL(file).href);
const declaration = (name, init) => variable("const", name, init);

function fixture(name, source, unitCount, moved) {
  const dir = path.join(root, name);
  const spec = {
    entry,
    modules: {
      "worker.mjs": moved.map((i) => "#" + i),
      [entry]: Array.from({ length: unitCount }, (_, i) => i)
        .filter((i) => !moved.includes(i))
        .map((i) => "#" + i),
    },
    order: ["worker.mjs"],
    sequential: true,
  };
  const files = prepare(dir, { source, spec, provider: "" });
  const out = path.join(dir, "certified");
  return { files, dir, out, ...lower(files.sourceFile, files.specFile, out) };
}

async function requireCertified({ verdict, log, files, out }, expected) {
  assertCertified(verdict, log);
  const original = await load(files.sourceFile);
  const emitted = await load(path.join(out, entry));
  assert.deepEqual(Object.keys(emitted), ["result"]);
  assert.equal(original.result, expected);
  assert.equal(emitted.result, expected);
}

// Vary static and computed member syntax, a numeric index and a dynamic key,
// and lexical this in an arrow called inside an ordinary function.
for (const [seed, value, computed, readKey, callKey, invoke] of [
  [0, "dot", false, id("field"), id("read"), (expr) => expr],
  [1, "computed", true, literal(0), call(arrow([], literal("read"))), (expr) => call(arrow([], expr))],
  [2, "computed string", true, literal("field"), literal("read"), (expr) => expr],
]) {
  const thisValue = { type: "ThisExpression" };
  const instance = id("instance");
  const body = [
    declaration("read", functionExpression([], block([returns(invoke(member(thisValue, readKey, computed)))]))),
    declaration("instance", {
      type: "ObjectExpression",
      properties: [
        { ...property("field", literal(value)), key: computed ? literal(String(readKey.value)) : readKey },
        property("read", id("read")),
      ],
    }),
    {
      type: "ExportNamedDeclaration",
      source: null,
      specifiers: [],
      declaration: declaration("result", call(member(instance, callKey, computed))),
    },
  ];
  const test = fixture(`generated-method-${seed}`, program(body), body.length, [0, 1]);
  await requireCertified(test, value);
}

// In the original, an early lexical read in the property expression is in the TDZ.
// Moving the declaration earlier changes what is caught, even though null has no
// properties. If the read is in an argument, the failed lookup skips it in both orders.
for (const computed of [false, true]) {
  const expression = computed ? "null[key()]()" : "null.method(key())";
  const test = fixture(
    `property-read-order-${computed}`,
    `
      const key = () => { later; throw 7; };
      const read = () => {
        try { ${expression}; } catch (e) { return typeof e; }
      };
      export const result = read();
      const later = 0;
    `,
    4,
    [3]
  );
  if (!computed) {
    await requireCertified(test, "object");
    continue;
  }
  const { verdict, log } = test;
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, /probe executions completed normally with different final states/);
  assert(!existsSync(test.out));
  const preview = path.join(test.dir, "preview");
  forcePreview(test.files, preview);
  assert.equal((await load(test.files.sourceFile)).result, "object");
  assert.equal((await load(path.join(preview, entry))).result, "number");
}

await requireCertified(
  fixture("optional", "const instance = { field: 1 }; export const result = instance?.field;", 2, [0]),
  1
);

{
  const test = fixture("object-key", "const instance = { field: 1 }; export const result = instance[{}];", 2, [0]);
  const orderKept = assertRejectedOrOrderKeptSlice(test, /object is converted to a primitive/);
  if (orderKept) {
    assert.equal((await load(test.files.sourceFile)).result, undefined);
    assert.equal((await load(path.join(test.out, entry))).result, undefined);
  }
}

// A wrong presence result must throw during initialization, even if both
// programs lower the expression the same way.
for (const [name, key, instance, present] of [
  [
    "own",
    "field",
    {
      type: "ObjectExpression",
      properties: [property("field", id("undefined"))],
    },
    true,
  ],
  ["inherited", "toString", { type: "ObjectExpression", properties: [] }, true],
  ["absent", "field", { type: "ObjectExpression", properties: [] }, false],
  ["index", 0, array([literal(null)]), true],
]) {
  const test = binary("in", literal(key), id("instance"));
  const unexpected = call(arrow([], block([throws(literal("wrong presence"))])));
  const expected = literal(present);
  const source = program([
    declaration("instance", instance),
    {
      type: "ExportNamedDeclaration",
      source: null,
      specifiers: [],
      declaration: declaration(
        "result",
        conditional(test, present ? expected : unexpected, present ? unexpected : expected)
      ),
    },
  ]);
  await requireCertified(fixture(`in-${name}`, source, 2, [0]), present);
}

await requireCertified(
  fixture(
    "in-primitive-base-before-key-conversion",
    `const worker = () => {
      const trace = { step: "start" };
      const key = () => {
        if (trace.step !== "start") throw "key repeated";
        trace.step = "key";
        return {};
      };
      const base = () => {
        if (trace.step !== "key") throw "base out of order";
        trace.step = "base";
        return 1;
      };
      try { return key() in base(); }
      catch (error) {
        if (typeof error !== "object") throw error;
        return trace.step;
      }
    };
    export const result = worker();`,
    2,
    [0]
  ),
  "base"
);

{
  const test = fixture(
    "in-object-key",
    `const instance = { "[object Object]": true }; export const result = {} in instance;`,
    2,
    [0]
  );
  const orderKept = assertRejectedOrOrderKeptSlice(test, /object is converted to a primitive/);
  if (orderKept) assert.equal((await load(path.join(test.out, entry))).result, true);
  assert.equal((await load(test.files.sourceFile)).result, true);
}

// The presence read distinguishes the initial object from its later mutation.
{
  const dir = path.join(root, "in-before-write");
  const files = prepare(dir, {
    source: `const instance = {};
      instance.key = true;
      const present = "key" in instance;
      export { present as result };`,
    provider: "",
    spec: {
      entry,
      modules: { "state.mjs": ["#0"], "write.mjs": ["#1"], "check.mjs": ["#2"], [entry]: ["#3"] },
      order: ["state.mjs", "check.mjs", "write.mjs"],
      sequential: true,
    },
  });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, /probe executions completed normally with different final states/);
  assert(!existsSync(out));
  const preview = path.join(dir, "preview");
  forcePreview(files, preview);
  assert.equal((await load(files.sourceFile)).result, true);
  assert.equal((await load(path.join(preview, entry))).result, false);
}
