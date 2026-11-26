import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const root = process.env.TEST_TMPDIR;
const dir = path.join(root, "cases", "routing");
mkdirSync(dir, { recursive: true });
writeFileSync(
  path.join(dir, "main.js"),
  `const a = 1, other = 2, b = 3;
if (true) { var hoisted = 4; }
if (true) { let blockLocal = 0; }
console.log(a + other + b + hoisted);`
);
writeFileSync(path.join(dir, "expect.yaml"), 'spec.yaml: {accept: {stdout: "10\\n"}}');
const spec = `logical_modules:
  main:
    scene/Shape: { members: [{selector: {binding: {name: a}}}] }
    tail: { members: [{selector: {binding: {name: b}}}] }
unassigned_mode: {main: {kind: catchall_file, target: Rest}}
import_order: {main: [SCENE/Shape, REST, tail]}
`;
const specPath = path.join(dir, "spec.yaml");
writeFileSync(specPath, spec);
const baseline = path.join(root, "baseline.jsonl");
const keep = path.join(root, "keep");
function run(args) {
  return spawnSync(path.resolve(process.env.CASE_RUNNER), ["--cases", path.dirname(dir), ...args], {
    encoding: "utf8",
  });
}
const good = run(["--out", baseline, "--keep", keep]);
assert.equal(good.status, 0, good.stderr);
assert.equal(JSON.parse(readFileSync(baseline)).outcome, "agree-accept");
const plan = JSON.parse(readFileSync(path.join(keep, "routing/spec/lean_spec.json")));
assert.deepEqual(plan.order, ["scene_shape.mjs", "rest.mjs", "tail.mjs"]);
assert.deepEqual(plan.modules["rest.mjs"], ["#1", "#3"]);
assert.deepEqual(plan.modules["entry.mjs"], ["#4", "#5"]);

writeFileSync(specPath, spec.replace("name: a", "name: missing"));
const lost = run(["--out", path.join(root, "broken.jsonl"), "--baseline", baseline, "--regression"]);
assert.equal(lost.status, 1, lost.stderr);
assert.match(lost.stderr, /routing\/spec: lost Lean coverage/);
