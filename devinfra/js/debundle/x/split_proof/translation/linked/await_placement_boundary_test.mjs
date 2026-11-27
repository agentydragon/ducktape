import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { split } from "../testing/split.mjs";

const root = process.env.TEST_TMPDIR;
const worker = `let trace = "";
let value = 0;
async function left() { await 0; trace += "A" + value; }
async function right() { await 0; trace += "B" + value; }
function setValue(next) { value = next; }
`;
const ending = `setValue(7);
const inTurn = trace;
export { trace, inTurn };
`;
const source = path.join(root, "source.mjs");
writeFileSync(source, worker + "left(); right();\n" + ending);
const observe = async (file) => {
  const { trace, inTurn } = await import(pathToFileURL(file).href);
  return { trace, inTurn };
};
assert.deepEqual(await observe(source), { trace: "A7B7", inTurn: "" });
for (const [label, calls, trace] of [
  ["preserve", "left(); right();", "A7B7"],
  ["reorder", "right(); left();", "B7A7"],
]) {
  const dir = path.join(root, label);
  mkdirSync(dir);
  writeFileSync(path.join(dir, "worker.mjs"), worker + "export { trace, left, right, setValue };\n");
  const entry = path.join(dir, "entry.mjs");
  writeFileSync(entry, 'import { trace, left, right, setValue } from "./worker.mjs";\n' + calls + "\n" + ending);
  assert.deepEqual(await observe(entry), { trace, inTurn: "" });
}

// The physical placements above are hand-authored controls. The production
// splitter cannot publish an admitted result for this suspension target.
const spec = path.join(root, "spec.json");
writeFileSync(
  spec,
  JSON.stringify({
    entry: "entry.mjs",
    modules: {
      "worker.mjs": ["#0", "#1", "#2", "#3", "#4"],
      "entry.mjs": ["#5", "#6", "#7", "#8", "#9"],
    },
    order: ["worker.mjs"],
    sequential: true,
  })
);
const out = path.join(root, "out");
const result = split({ source, spec, out, require: "all-outcomes" });
assert.equal(result.status, "refused", result.log);
assert.equal(result.reasonCode, "no_constructed_rule", result.log);
assert.deepEqual(result.files, []);
