import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { forcePreview, lower, observe, prepare, root } from "./harness.mjs";

for (const [name, object, action, expected, accepted] of [
  ["alias-read", "{value: 1}", "alias.value;", 1, true],
  ["alias-write", "{value: 1}", "alias.value = 2;", 2, false],
  ["getter", "{get value() { return 3; }}", "alias.value;", 3, false],
  ["inherited", "{__proto__: {value: 4}}", "alias.value;", 4, false],
]) {
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source: `const table = ${object}; const alias = table; ${action} const chosen = table.value; export { table, chosen };`,
    provider: "",
    spec: { entry: "entry.mjs", modules: { "entry.mjs": ["#1", "#2", "#4"], "records.mjs": ["#0", "#3"] } },
  });
  const out = path.join(dir, "strict");
  const run = (...flags) =>
    lower(
      files.sourceFile,
      files.specFile,
      out,
      ["--structural", ...flags],
      accepted ? "linked-structural-pivot" : "linked-structural"
    ).verdict;
  const verdict = run();
  assert.deepEqual(observe(files.sourceFile, "chosen"), { chosen: expected });
  if (accepted) {
    assert.equal(verdict.status, "certified", JSON.stringify(verdict));
    assert.deepEqual(run("--check-output"), verdict);
    assert.deepEqual(observe(path.join(out, "entry.mjs"), "chosen"), { chosen: expected });
  } else {
    assert.equal(verdict.status, "rejected");
    assert(!existsSync(out));
    if (name === "alias-write") {
      const preview = path.join(dir, "preview");
      forcePreview(files, preview);
      assert.deepEqual(observe(path.join(preview, "entry.mjs"), "chosen"), { chosen: 1 });
    }
  }
}

// In the original module the producer runs first. Its presence in the source
// cannot justify moving the read into a module that initializes first.
{
  const dir = path.join(root, "before-initialization");
  const files = prepare(dir, {
    source: "const table = {value: 1}; const chosen = table.value; export {table, chosen};",
    provider: "",
    spec: { entry: "entry.mjs", modules: { "entry.mjs": ["#0", "#2"], "records.mjs": ["#1"] } },
  });
  const out = path.join(dir, "out");
  assert.equal(
    lower(files.sourceFile, files.specFile, out, ["--structural"], "linked-structural").verdict.status,
    "rejected"
  );
  assert(!existsSync(out));
  assert.deepEqual(observe(files.sourceFile, "chosen"), { chosen: 1 });
  const preview = path.join(dir, "preview");
  forcePreview(files, preview);
  assert.deepEqual(observe(path.join(preview, "entry.mjs"), "chosen"), { error: "ReferenceError" });
}
