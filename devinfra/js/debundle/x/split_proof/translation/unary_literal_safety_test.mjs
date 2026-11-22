// Unary operators on literals are not effects, so the statement that holds one may move.
//
// Not covered, because the checker assumes the opposite: `+1n` always throws (premise P4: evaluation completes), and a
// `Symbol.toPrimitive` patched onto `RegExp.prototype` by another script runs when `+/x/` converts (premise P1: the
// intrinsics are not patched).
import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { lowerDump } from "../lowering/dump_core.mjs";

const temp = process.env.TEST_TMPDIR;
assert(temp);
const run = (bin, args) => spawnSync(bin, args, { encoding: "utf8" });
function prepare(name, code, side) {
  const dir = path.join(temp, name);
  mkdirSync(dir, { recursive: true });
  const source = path.join(temp, `${name}.mjs`);
  const input = path.join(temp, `${name}-ast.json`);
  const spec = path.join(temp, `${name}-spec.json`);
  const parsed = acorn.parse(code, { sourceType: "module", ecmaVersion: "latest" });
  writeFileSync(source, code);
  writeFileSync(input, JSON.stringify(lowerDump(code, acorn, eslintScope)));
  writeFileSync(
    spec,
    JSON.stringify({
      entry: "entry.mjs",
      modules: {
        "entry.mjs": parsed.body.map((_, i) => `#${i}`).filter((_, i) => !side.includes(i)),
        "side.mjs": side.map((i) => `#${i}`),
      },
    })
  );
  return { dir, source, input, spec };
}

for (const [name, expression] of [
  ["number_plus", "+1"],
  ["string_minus", '-"1"'],
  ["bigint_minus", "-1n"],
  ["bigint_bitnot", "~1n"],
  ["regexp_not", "!/x/"],
  ["number_bitnot", "~1"],
]) {
  const code = `const watched = 7;\nconst value = ${expression};\nfunction get() { return watched; }\nconsole.log(get(),String(value));`;
  const { dir, source, input, spec } = prepare(`safe_${name}`, code, [1]);
  const checked = run(process.env.LOWERSPLIT, [input, spec, dir]);
  assert.equal(checked.status, 0, `${name}: ${checked.stdout}\n${checked.stderr}`);
  assert(existsSync(path.join(dir, "entry.mjs")), name);
  const before = run(process.execPath, [source]),
    after = run(process.execPath, [path.join(dir, "entry.mjs")]);
  assert.equal(before.status, 0, before.stderr);
  assert.equal(after.status, 0, after.stderr);
  assert.equal(after.stdout, before.stdout, name);
}
