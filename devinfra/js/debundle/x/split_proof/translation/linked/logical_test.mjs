import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { split } from "../testing/split.mjs";
import { observe, prepare, root } from "./harness.mjs";

// A selected right operand reads a later lexical binding in the source. Moving
// that binding before the read must not turn the TDZ failure into success.
for (const [operator, evaluateRight] of [
  ["&&", true],
  ["||", false],
  ["??", null],
]) {
  const dir = path.join(root, `tdz-${operator}`);
  const files = prepare(dir, {
    source: `const value = ${JSON.stringify(evaluateRight)} ${operator} late; const late = "ready"; export { value as result };`,
    provider: "",
    spec: {
      entry: "entry.mjs",
      modules: { "use.mjs": ["#0"], "late.mjs": ["#1"], "entry.mjs": ["#2"] },
      order: ["late.mjs", "use.mjs"],
      sequential: true,
    },
  });
  assert.deepEqual(observe(files.sourceFile, "result"), { error: "ReferenceError" });
  const out = path.join(dir, "out");
  const request = { source: files.sourceFile, spec: files.specFile, out, require: "all-outcomes" };
  const refused = split(request);
  assert.equal(refused.status, "refused", refused.log);
  assert(!existsSync(out));
  mkdirSync(out);
  const sentinel = path.join(out, "sentinel.txt");
  writeFileSync(sentinel, "keep these bytes\n");
  const existing = split({ ...request, allowExistingOut: true });
  assert.equal(existing.status, "refused", existing.log);
  assert.equal(readFileSync(sentinel, "utf8"), "keep these bytes\n");
}
