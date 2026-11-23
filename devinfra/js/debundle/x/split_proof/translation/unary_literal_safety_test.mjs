// Unary operators on literals are not effects, so the statement that holds one may move.
//
// Not covered, because the checker assumes the opposite: `+1n` always throws (premise P4: evaluation completes), and a
// `Symbol.toPrimitive` patched onto `RegExp.prototype` by another script runs when `+/x/` converts (premise P1: the
// intrinsics are not patched).
import { strict as assert } from "node:assert";
import { existsSync } from "node:fs";
import path from "node:path";
import { lowerDiagnostic, prepareSideSplit, run } from "./testing/diagnostic_split.mjs";

for (const [name, expression] of [
  ["number_plus", "+1"],
  ["string_minus", '-"1"'],
  ["bigint_minus", "-1n"],
  ["bigint_bitnot", "~1n"],
  ["regexp_not", "!/x/"],
  ["number_bitnot", "~1"],
]) {
  const code = `const watched = 7;\nconst value = ${expression};\nfunction get() { return watched; }\nconsole.log(get(),String(value));`;
  const fixture = prepareSideSplit(`safe_${name}`, code, [1]);
  const { dir, source } = fixture;
  const checked = lowerDiagnostic(fixture);
  assert.equal(checked.status, 0, `${name}: ${checked.stdout}\n${checked.stderr}`);
  assert(existsSync(path.join(dir, "entry.mjs")), name);
  const before = run(process.execPath, [source]),
    after = run(process.execPath, [path.join(dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.equal(after.status, 0, after.stderr);
  assert.equal(after.stdout, before.stdout, name);
}
