import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const temp = process.env.TEST_TMPDIR;
// Node control for the external-module cycle: `lowersplit` rejects import syntax, so these source modules exercise the
// ESM behavior being guarded directly.
for (const [name, reordered] of [
  ["cycle_original", false],
  ["cycle_reordered", true],
]) {
  const dir = path.join(temp, name);
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    path.join(dir, "observer.mjs"),
    `import { local } from "./entry.mjs";\nglobalThis.hook = () => console.log(local);`
  );
  writeFileSync(path.join(dir, "side.mjs"), `globalThis.hook();`);
  writeFileSync(
    path.join(dir, "entry.mjs"),
    reordered
      ? `import "./observer.mjs";\nimport "./side.mjs";\nexport const local = 7;`
      : `import "./observer.mjs";\nexport const local = 7;\nglobalThis.hook();`
  );
  const result = spawnSync(process.execPath, [path.join(dir, "entry.mjs")], { encoding: "utf8" });
  if (reordered) {
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /ReferenceError/);
  } else {
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, "7\n");
  }
}
