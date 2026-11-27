import assert from "node:assert/strict";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { observe, root, structuralSplit } from "./harness.mjs";

const plain = path.join(root, "page.json");
const stable = path.join(root, "pi.json");
writeFileSync(plain, JSON.stringify({ profile: "browser-page" }));
writeFileSync(stable, JSON.stringify({ profile: "browser-page", stableIntrinsicValues: ["Math.PI@1"] }));
const pivot = "linked-structural-pivot";
const refusal = "linked-structural";

function fixture(name, before, operand) {
  return structuralSplit(name, `${before}; var result = ${operand} / 12; export { result };`, {
    "entry.mjs": ["#0", "#2"],
    "angle.mjs": ["#1"],
  });
}

for (const [name, before, operand, expected] of [
  ["ordinary", "var marker = 1", "Math.PI", Math.PI / 12],
  ["replaced", "globalThis.Math = { PI: 24 }", "Math.PI", 2],
  ["getter", "globalThis.Math = { get PI() { return 36; } }", "Math.PI", 3],
  ["coercion", "Object.prototype.valueOf = () => 48", "({})", 4],
]) {
  const test = fixture(name, before, operand);
  const { out, run } = test;
  assert.equal(run(refusal).status, "rejected");
  assert.match(run(refusal, "--host-contract", plain).reason, /unit 1 is not a local allocating declaration/);
  assert(!existsSync(out));
  if (name === "ordinary") {
    const emitted = run(pivot, "--host-contract", stable);
    assert.equal(emitted.status, "certified", JSON.stringify(emitted));
    assert.match(emitted.assumptions.join("\n"), /Math.*window\.Math.*PI.*4614256656552045848.*no observable effects/);
    assert.deepEqual(run(pivot, "--check-output", "--host-contract", stable), emitted);
    assert.equal(run(refusal, "--check-output", "--host-contract", plain).status, "rejected");
    assert.deepEqual(observe(path.join(out, "entry.mjs"), "result"), { result: expected });
  } else if (name === "coercion") {
    assert.equal(run(refusal, "--host-contract", stable).status, "rejected");
  }
  // Keep the direct Node observation of the original program. Refused moves
  // publish no preview, so the only emitted output is the certified ordinary case above.
  assert.deepEqual(observe(test.sourceFile, "result"), { result: expected });
}

const shadow = fixture("lexical", "export const Math = { get PI() { return 24; } }", "Math.PI");
assert.match(shadow.run(refusal, "--host-contract", stable).reason, /unit 1 is not a local allocating declaration/);
assert(!existsSync(shadow.out));
