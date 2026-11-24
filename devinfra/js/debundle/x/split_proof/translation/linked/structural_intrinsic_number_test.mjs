import assert from "node:assert/strict";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { forcePreview, lower, observe, prepare, root } from "./harness.mjs";

const plain = path.join(root, "page.json");
const stable = path.join(root, "pi.json");
writeFileSync(plain, JSON.stringify({ profile: "browser-page" }));
writeFileSync(stable, JSON.stringify({ profile: "browser-page", stableIntrinsicValues: ["Math.PI@1"] }));
const pivot = "linked-structural-pivot";
const refusal = "linked-structural";

function fixture(name, before, operand) {
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source: `${before}; var result = ${operand} / 12; export { result };`,
    provider: "",
    spec: { entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#2"], "angle.mjs": ["#1"] } },
  });
  const out = path.join(dir, "out");
  const run = (scope, ...args) =>
    lower(files.sourceFile, files.specFile, out, ["--structural", ...args], scope).verdict;
  return { dir, files, out, run };
}

for (const [name, before, operand, expected, moved] of [
  ["ordinary", "var marker = 1", "Math.PI", Math.PI / 12, Math.PI / 12],
  ["replaced", "globalThis.Math = { PI: 24 }", "Math.PI", 2, Math.PI / 12],
  ["getter", "globalThis.Math = { get PI() { return 36; } }", "Math.PI", 3, Math.PI / 12],
  ["coercion", "Object.prototype.valueOf = () => 48", "({})", 4, null],
]) {
  const { dir, files, out, run } = fixture(name, before, operand);
  assert.equal(run(refusal).status, "rejected");
  assert.match(run(refusal, "--host-contract", plain).reason, /unit 1 is not a local allocating declaration/);
  assert(!existsSync(out));
  if (name === "ordinary") {
    const emitted = run(pivot, "--host-contract", stable);
    assert.equal(emitted.status, "certified", JSON.stringify(emitted));
    assert.match(emitted.assumptions.join("\n"), /Math.*window\.Math.*PI.*4614256656552045848.*no observable effects/);
    assert.deepEqual(run(pivot, "--check-output", "--host-contract", stable), emitted);
    assert.equal(run(refusal, "--check-output", "--host-contract", plain).status, "rejected");
    assert.deepEqual(observe(path.join(out, "entry.mjs"), "result"), { result: moved });
  } else if (name === "coercion") {
    assert.equal(run(refusal, "--host-contract", stable).status, "rejected");
  }
  // The forced preview exposes what the unconditional move would do in a
  // deployment that violates the stated premise. Observe each in a fresh realm.
  const preview = path.join(dir, "preview");
  forcePreview(files, preview);
  assert.deepEqual(observe(files.sourceFile, "result"), { result: expected });
  assert.deepEqual(observe(path.join(preview, "entry.mjs"), "result"), { result: moved });
}

const shadow = fixture("lexical", "export const Math = { get PI() { return 24; } }", "Math.PI");
assert.match(shadow.run(refusal, "--host-contract", stable).reason, /unit 1 is not a local allocating declaration/);
assert(!existsSync(shadow.out));
