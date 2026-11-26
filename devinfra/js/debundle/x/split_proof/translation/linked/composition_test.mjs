import assert from "node:assert/strict";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  assertCertified,
  array,
  block,
  exportNamed,
  id,
  literal,
  lower,
  member,
  moduleEvaluationOrder,
  prepare,
  program,
  property,
  returns,
  root,
  variable,
} from "./harness.mjs";

const record = (properties) => ({ type: "ObjectExpression", properties });
const spread = (name) => ({ type: "SpreadElement", argument: id(name) });
const read = (object, field) => member(id(object), id(field));

const entry = "entry.mjs";
const order = ["table.mjs", "right.mjs", "left.mjs", "meta.mjs"];
const spec = {
  entry,
  modules: {
    "table.mjs": ["#0"],
    "meta.mjs": ["#1"],
    "left.mjs": ["#2"],
    "right.mjs": ["#3"],
    [entry]: ["#4", "#5", "#6"],
  },
  order,
  sequential: true,
};

// Reverse the initialization order of the two arrays. The deferred builder
// still spreads them in source order and preserves the identity of each record.
for (const [width, reversedKeys, readPosition] of [
  [65, false, 0],
  [67, true, 65],
]) {
  const keys = Array.from({ length: width }, (_, i) => `field${i}`);
  if (reversedKeys) keys.reverse();
  const selected = keys[readPosition];
  const middle = keys[Math.floor(width / 2)];
  const value = (key) => `${width}-${key}`;
  const item = (key) => record([property("value", read("table", key))]);
  const builder = `makeResult${width}`;
  const source = program([
    variable("const", "table", record(keys.map((key) => property(key, literal(value(key)))))),
    variable("const", "meta", record([property("mode", read("table", selected))])),
    variable("const", "left", array([item(middle), item(selected)])),
    variable("const", "right", array([item(selected)])),
    variable("const", "before", id(builder)),
    {
      type: "FunctionDeclaration",
      id: id(builder),
      params: [],
      body: block([
        returns(
          record([property("mode", read("meta", "mode")), property("items", array([spread("left"), spread("right")]))])
        ),
      ]),
      async: false,
      generator: false,
    },
    exportNamed([
      ["before", "before"],
      [builder, "build"],
      ["left", "left"],
      ["right", "right"],
      ["table", "table"],
    ]),
  ]);
  const dir = path.join(root, `generated-composition-${width}`);
  const { sourceFile, specFile } = prepare(dir, { source, provider: "", spec });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(sourceFile, specFile, out);
  assertCertified(verdict, log);
  assert.equal(verdict.strategy, "compiled-replay-premised", log);
  assert.deepEqual(moduleEvaluationOrder(out, entry), [...order, entry]);

  for (const file of [sourceFile, path.join(out, entry)]) {
    const ns = await import(pathToFileURL(file).href);
    assert.deepEqual(Object.keys(ns), ["before", "build", "left", "right", "table"]);
    assert.deepEqual(Object.keys(ns.table), keys);
    assert.equal(ns.build.name, builder);
    assert.equal(ns.before, ns.build);

    const first = ns.build();
    const second = ns.build();
    assert.deepEqual(first, {
      mode: value(selected),
      items: [middle, selected, selected].map((key) => ({ value: value(key) })),
    });
    assert.deepEqual(second, first);
    assert.notEqual(first.items, second.items);
    assert.notEqual(first.items[1], first.items[2]);
    for (const [index, item] of [...ns.left, ...ns.right].entries()) {
      assert.equal(first.items[index], item);
      assert.equal(second.items[index], item);
    }
  }
}

// An eager getter can mutate bindings. Until calls are replayed, keep the
// generated proof for this read and preserve its observable result.
{
  const dir = path.join(root, "getter-fallback");
  const source = `let count = 0;
    const table = { get value() { count += 1; return count; } };
    const chosen = table.value;
    export { chosen, count };`;
  const files = prepare(dir, {
    source,
    provider: "",
    spec: {
      entry,
      modules: { "table.mjs": ["#0", "#1"], [entry]: ["#2", "#3"] },
      order: ["table.mjs"],
      sequential: true,
    },
  });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  assertCertified(verdict, log);
  assert.equal(verdict.strategy, "generated-proof", log);
  assert(verdict.replay_blockers.includes("property-read"), log);
  assert(verdict.replay_blockers.includes("object-accessor"), log);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    const result = await import(pathToFileURL(file).href);
    assert.deepEqual(Object.keys(result), ["chosen", "count"]);
    assert.equal(result.chosen, 1);
    assert.equal(result.count, 1);
  }
}
