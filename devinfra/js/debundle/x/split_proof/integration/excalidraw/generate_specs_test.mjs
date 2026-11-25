import assert from "node:assert/strict";
import { leanSpec } from "./generate_specs.mjs";

const graph = {
  chunk_id: "test",
  module_graph: {
    nodes: [
      { key: "entry", kind: "entry", path: "residual" },
      { key: "part", kind: "specified", path: "example/part" },
    ],
  },
  nodes: [
    { id: "unit:5", statement_ordinal: 1, destination: "part" },
    { id: "unit:8", statement_ordinal: 0, destination: "entry" },
  ],
};
assert.deepEqual(leanSpec(graph, [5, 8]), {
  entry: "entry.mjs",
  modules: { "entry.mjs": ["#0"], "example/part.mjs": ["#1"] },
  order: ["example/part.mjs"],
  sequential: true,
});

const unknownOwner = structuredClone(graph);
unknownOwner.nodes[0].destination = "missing";
assert.throws(() => leanSpec(unknownOwner, [5, 8]), assert.AssertionError);

const invalidOwner = structuredClone(graph);
invalidOwner.module_graph.nodes[1].path = "../part";
assert.throws(() => leanSpec(invalidOwner, [5, 8]), assert.AssertionError);

for (const invalidSchedule of [undefined, [5, 9], [5, 5], [5], [8, 5]]) {
  assert.throws(() => leanSpec(graph, invalidSchedule), assert.AssertionError);
}
