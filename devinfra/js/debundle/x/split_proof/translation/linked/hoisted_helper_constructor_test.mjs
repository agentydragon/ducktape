import assert from "node:assert/strict";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { split } from "../testing/split.mjs";
import { moduleEvaluationOrder, observe } from "./harness.mjs";

const entry = "entry.mjs";
const helper = "helper.mjs";
function fixture(name, source, helperIds, entryIds) {
  const dir = path.join(process.env.TEST_TMPDIR, name);
  mkdirSync(dir, { recursive: true });
  const sourceFile = path.join(dir, "source.mjs");
  writeFileSync(sourceFile, source);
  const placement = { entry, modules: { [helper]: helperIds, [entry]: entryIds }, order: [helper], sequential: true };
  const spec = (value, filename = "placement.json") => {
    const file = path.join(dir, filename);
    writeFileSync(file, JSON.stringify(value));
    return file;
  };
  const specFile = spec(placement);
  const out = path.join(dir, "out");
  const run = (options = {}) => split({ source: sourceFile, spec: specFile, out, require: "all-outcomes", ...options });
  return { dir, sourceFile, out, placement, run, spec };
}

function check(fixture, resultName, value, helperText) {
  const result = fixture.run();
  assert.equal(result.status, "certified", result.log);
  assert.equal(result.scope, "ordered-helper");
  assert.equal(result.guarantee, "all-outcomes");
  assert.equal(result.verdict.strategy, "compiled-constructor");
  assert.deepEqual(result.files, [entry, helper]);
  assert.deepEqual(moduleEvaluationOrder(fixture.out, entry), [helper, entry]);
  assert(readFileSync(path.join(fixture.out, helper), "utf8").includes(helperText));
  const expected = { [resultName]: value };
  assert.deepEqual(observe(fixture.sourceFile, [resultName]), expected);
  assert.deepEqual(observe(path.join(fixture.out, entry), [resultName]), expected);
  const reread = fixture.run({ checkOutput: true });
  assert.equal(reread.status, "certified", reread.log);
  assert.deepEqual(reread.verdict, result.verdict);
}

const simple = fixture("simple", "function f(){return 1} export const y=f();", ["#0"], ["#1"]);
check(simple, "y", 1, "function f(){return 1}");

const capturedSource = `let seed = 7;
function read() { return seed; }
export const result = read();
`;
const captured = fixture("captured", capturedSource, ["#0", "#1"], ["#2"]);
check(captured, "result", 7, "function read() { return seed; }");

const moved = path.join(captured.out, helper);
const movedText = readFileSync(moved, "utf8");
writeFileSync(moved, movedText.replace("return seed;", "return 7;"));
assert.deepEqual(observe(path.join(captured.out, entry), ["result"]), { result: 7 });
const wrongCapture = captured.run({ checkOutput: true });
assert.equal(wrongCapture.status, "refused", wrongCapture.log);
assert.match(wrongCapture.reason, /output module helper\.mjs differs/);

const wrongSpec = captured.spec(
  { ...captured.placement, modules: { [helper]: ["#1"], [entry]: ["#0", "#2"] } },
  "wrong-owner.json"
);
for (const options of [{ out: path.join(captured.dir, "wrong-owner") }, { allowExistingOut: true }]) {
  const refused = captured.run({ spec: wrongSpec, ...options });
  assert.equal(refused.status, "refused", refused.log);
}

const tdz = fixture("tdz", "function f(){return seed} export const y=f(); let seed=1;", ["#0", "#2"], ["#1"]);
assert.deepEqual(observe(tdz.sourceFile, "y"), { error: "ReferenceError" });
const tdzRefusal = tdz.run();
assert.equal(tdzRefusal.status, "refused", tdzRefusal.log);

const effectful = fixture(
  "effectful",
  'function f(){console.log("effect");return 1} export const y=f();',
  ["#0"],
  ["#1"]
);
const effectRefusal = effectful.run();
assert.equal(effectRefusal.status, "refused", effectRefusal.log);
