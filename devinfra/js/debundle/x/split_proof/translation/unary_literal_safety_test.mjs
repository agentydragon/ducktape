// Unary operators on literals are not effects, so the statement that holds one may move.
//
// Not covered, because the checker assumes the opposite: `+1n` always throws (premise P4: evaluation completes), and a
// `Symbol.toPrimitive` patched onto `RegExp.prototype` by another script runs when `+/x/` converts (premise P1: the
// intrinsics are not patched).
import { strict as assert } from "node:assert";
import { existsSync } from "node:fs";
import path from "node:path";
import { prepareSideSplit, run } from "./testing/diagnostic_split.mjs";
import { split } from "./testing/split.mjs";

const certification = { certified: 0, refused: 0 };
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
  const { source } = fixture;
  const checked = split({
    ast: fixture.input,
    spec: fixture.spec,
    out: fixture.certifiedOut,
    hostContract: fixture.hostContract,
  });
  let output;
  if (checked.status === "certified") {
    certification.certified++;
    output = fixture.certifiedOut;
  } else {
    certification.refused++;
    assert.equal(checked.files.length, 0, checked.log);
    assert(!existsSync(path.join(fixture.certifiedOut, "entry.mjs")), checked.log);
    continue;
  }
  assert(existsSync(path.join(output, "entry.mjs")), name);
  const before = run(process.execPath, [source]),
    after = run(process.execPath, [path.join(output, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.equal(after.status, 0, after.stderr);
  assert.equal(after.stdout, before.stdout, name);
}
assert.equal(certification.certified + certification.refused, 6);
assert(certification.certified > 0, "at least one unary form should remain in the certified fragment");
assert(certification.refused > 0, "unsupported unary forms should be counted as certified refusals");
console.log(`unary split certification: ${JSON.stringify(certification)}`);
