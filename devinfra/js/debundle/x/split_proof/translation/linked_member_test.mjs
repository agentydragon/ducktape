import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  arrow,
  block,
  call,
  forcePreview,
  functionExpression,
  id,
  literal,
  lower,
  prepare,
  program,
  property,
  returns,
  root,
  variable,
} from "./linked_programs_test_support.mjs";

const entry = "entry.mjs";
const load = (file) => import(pathToFileURL(file).href);
const member = (object, property, computed) => ({
  type: "MemberExpression",
  object,
  property,
  computed,
  optional: false,
});
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
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", log);
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

for (const [name, expr, reason] of [
  ["optional", "instance?.field", /unsupported linked expression ChainExpression/],
  ["object-key", "instance[{}]", /object is converted to a primitive/],
]) {
  const test = fixture(name, `const instance = { field: 1 }; export const result = ${expr};`, 2, [0]);
  const { verdict, log } = test;
  assert.equal(verdict.status, "rejected", log);
  assert.match(verdict.reason, reason);
  assert(!existsSync(test.out));
}
