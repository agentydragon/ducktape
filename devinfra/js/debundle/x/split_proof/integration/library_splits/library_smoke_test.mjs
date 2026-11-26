import assert from "node:assert/strict";
import path from "node:path";
import { smoke } from "./smoke.mjs";

const sources = process.argv.slice(2);
assert(sources.length > 0, "expected original library bundles");

for (const source of sources) {
  const library = path.basename(source, ".mjs");
  const result = await smoke(library, source);
  assert.equal(result.ok, true, `${library}: ${JSON.stringify(result)}`);

  if (library === "acorn") {
    assert.equal(result.value.program.type, "Program");
    assert.equal(result.value.module.sourceType, "module");
    assert.equal(result.value.expression.type, "BinaryExpression");
    assert.equal(result.value.tokens.length > 0, true);
    assert.equal(result.value.errors.length, 4);
    assert(result.value.errors.every((error) => error.startsWith("SyntaxError:")));
  } else if (library === "semver") {
    assert.deepEqual(result.value.valid, ["1.2.3", null, "2.0.0-beta.1+build"]);
    assert.deepEqual(result.value.compare, [true, true, -1]);
    assert.deepEqual(result.value.satisfies, [true, false, true]);
    assert.equal(result.value.maxSatisfying, "1.9.9");
  }
}
