import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { classify, coverageLosses } from "./verdict.mjs";

const expected = { accept: { stdout: "ready\n" } };
const mine = { verdict: "accept" };
function execute(source) {
  const { stdout, status: code } = spawnSync(process.execPath, ["-e", source], { encoding: "utf8" });
  return { stdout, code };
}
const original = execute('console.log("ready")');
const emitted = execute('console.log("ready"); throw new Error("after output")');
assert.equal(classify(expected, mine, { original, emitted: original }), "agree-accept");
assert.equal(classify(expected, mine, { original, emitted }), "unsound-accept");
assert.equal(classify(expected, mine, { original: emitted, emitted }), "error:original-baseline");

const prior = [{ id: "one", mine, outcome: "agree-accept" }];
const refusal = { id: "one", outcome: "unsupported:resolver refusal" };
assert.equal(coverageLosses(prior, [refusal], { filtered: true }).length, 1);
assert.equal(coverageLosses(prior, [], { filtered: false }).length, 1);
assert.deepEqual(coverageLosses(prior, [], { filtered: true }), []);
assert.deepEqual(coverageLosses(prior, prior, { filtered: false }), []);
