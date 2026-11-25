import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { forcePreview, observe, structuralSplit } from "./harness.mjs";

for (const [name, object, action, expected, accepted] of [
  ["alias-read", "{value: 1}", "alias.value;", 1, true],
  ["alias-write", "{value: 1}", "alias.value = 2;", 2, false],
  ["getter", "{get value() { return 3; }}", "alias.value;", 3, false],
  ["inherited", "{__proto__: {value: 4}}", "alias.value;", 4, false],
]) {
  const test = structuralSplit(
    name,
    `const table = ${object}; const alias = table; ${action} const chosen = table.value; export { table, chosen };`,
    { "entry.mjs": ["#1", "#2", "#4"], "records.mjs": ["#0", "#3"] }
  );
  const scope = accepted ? "linked-structural-pivot" : "linked-structural";
  const verdict = test.run(scope);
  assert.deepEqual(observe(test.sourceFile, "chosen"), { chosen: expected });
  if (accepted) {
    assert.equal(verdict.status, "certified", JSON.stringify(verdict));
    assert.deepEqual(test.run(scope, "--check-output"), verdict);
    assert.deepEqual(observe(path.join(test.out, "entry.mjs"), "chosen"), { chosen: expected });
  } else {
    assert.equal(verdict.status, "rejected");
    assert(!existsSync(test.out));
    if (name === "alias-write") {
      const preview = path.join(test.dir, "preview");
      forcePreview(test, preview);
      assert.deepEqual(observe(path.join(preview, "entry.mjs"), "chosen"), { chosen: 1 });
    }
  }
}

// In the original module the producer runs first. Its presence in the source
// cannot justify moving the read into a module that initializes first.
{
  const test = structuralSplit(
    "before-initialization",
    "const table = {value: 1}; const chosen = table.value; export {table, chosen};",
    { "entry.mjs": ["#0", "#2"], "records.mjs": ["#1"] }
  );
  assert.equal(test.run("linked-structural").status, "rejected");
  assert(!existsSync(test.out));
  assert.deepEqual(observe(test.sourceFile, "chosen"), { chosen: 1 });
  const preview = path.join(test.dir, "preview");
  forcePreview(test, preview);
  assert.deepEqual(observe(path.join(preview, "entry.mjs"), "chosen"), { error: "ReferenceError" });
}
