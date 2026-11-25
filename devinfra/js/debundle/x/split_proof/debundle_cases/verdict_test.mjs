import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { classify } from "./verdict.mjs";

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
