import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dump } from "../parser.mjs";

const source = `let seed = 7;
function read() { return seed; }
export const result = read();
`;
const original = await import("data:text/javascript," + encodeURIComponent(source));
assert.equal(original.result, 7);

const spec = {
  entry: "entry.mjs",
  modules: { "helper.mjs": ["#0", "#1"], "entry.mjs": ["#2"] },
  order: ["helper.mjs"],
  sequential: true,
};
function probe(placement) {
  const command = spawnSync(process.env.HOISTED_HELPER_CHECKER, [], {
    encoding: "utf8",
    input: JSON.stringify({ ast: dump(source), spec: JSON.stringify(placement) }) + "\n",
  });
  assert.ifError(command.error);
  return command;
}
const linked = probe(spec);
assert.equal(linked.status, 0, linked.stderr);
const [owners, order] = JSON.parse(linked.stdout);
assert.deepEqual(
  owners.sort(([a], [b]) => a - b),
  [
    [0, "helper.mjs"],
    [1, "helper.mjs"],
    [2, "entry.mjs"],
  ]
);
assert.deepEqual(order, ["helper.mjs", "entry.mjs"]);

// Keep the reader in the provider but place its capture in the entry. That
// changes the original unit order even though function hoisting may permit it.
const changedOrder = probe({ ...spec, modules: { "helper.mjs": ["#1"], "entry.mjs": ["#0", "#2"] } });
assert.equal(changedOrder.status, 1, changedOrder.stdout);
assert.match(changedOrder.stderr, /does not enumerate every original source ordinal in order/);
