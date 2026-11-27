import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import * as acorn from "acorn";
import { topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

const source = readFileSync(process.argv[2], "utf8");
const digest = (code) => createHash("sha256").update(code).digest("hex");
const sourceSha256 = digest(source);
assert.equal(sourceSha256, "e2478dfecff35578381298798b067b66c0424057a99c4c6ddddc6f6b2fb45eee");
const units = topLevelStatements(acorn.parse(source, { ecmaVersion: "latest", sourceType: "module" }));
assert.equal(units.length, 447);
const declarations = units.slice(0, 4).map(({ node, start, end }) => {
  // These are complete, independent variable statements, not declarator
  // fragments. Copy the exact source range; only the observation is new.
  assert.equal(node.type, "VariableDeclaration");
  assert.equal(node.kind, "var");
  assert.equal(node.declarations.length, 1);
  assert.equal(node.start, start);
  assert.equal(node.end, end);
  const [{ id, init }] = node.declarations;
  assert.equal(id.type, "Identifier");
  return { name: id.name, init, text: source.slice(start, end) };
});
for (const { init } of declarations.slice(0, 2)) {
  assert.equal(init.type, "ArrayExpression");
  assert(
    init.elements.length > 0 && init.elements.every((node) => node?.type === "Literal" && Number.isInteger(node.value))
  );
}
for (const { init } of declarations.slice(2)) {
  assert.equal(init.type, "Literal");
  assert.equal(typeof init.value, "string");
}

const tmp = process.env.TEST_TMPDIR;
const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const results = [];
async function trial(name, ordinals, sourceSuffix, placement, observe) {
  const text = ordinals.map((i) => declarations[i].text).join("\n") + "\n" + sourceSuffix + "\n";
  const input = path.join(artifacts, name + ".mjs");
  const spec = path.join(artifacts, name + ".json");
  const out = path.join(tmp, name);
  writeFileSync(input, text);
  writeFileSync(spec, JSON.stringify(placement, null, 2));
  const request = { source: input, spec, out, require: "all-outcomes" };
  const original = observe(await import(pathToFileURL(input).href));
  const result = split(request);
  if (result.status === "certified") {
    assert.equal(result.guarantee, "all-outcomes", result.log);
    assert.equal(result.verdict.strategy, "compiled-constructor", result.log);
    assert.deepEqual(split({ ...request, checkOutput: true }).verdict, result.verdict);
    assert.deepEqual(observe(await import(pathToFileURL(path.join(out, placement.entry)).href)), original);
  } else {
    assert.equal(result.reasonCode, "no_constructed_rule", result.log);
  }
  const row = {
    name,
    sourceSha256,
    projectionSha256: digest(text),
    ordinals,
    bindings: ordinals.map((i) => declarations[i].name),
    verdict: result.verdict,
    files: result.files,
    original,
  };
  results.push(row);
  console.log(JSON.stringify(row));
  return result.status;
}

const kept = {
  entry: "m1.mjs",
  modules: { "m0.mjs": ["#0", "#1"], "m1.mjs": ["#2"] },
  order: ["m0.mjs"],
  sequential: true,
};
const advance = {
  entry: "entry.mjs",
  modules: { "entry.mjs": ["#0", "#2"], "moved.mjs": ["#1"] },
  order: ["moved.mjs"],
  sequential: true,
};
const [first, second, , literal] = declarations;
const literalStatus = await trial(
  "literal-vars-kept",
  [2, 3],
  "export const observed = " + literal.name + ";",
  kept,
  (m) => ({ names: Object.keys(m), length: m.observed.length, sha256: digest(m.observed) })
);
if (process.env.EXPECT_LITERAL_VARS) assert.equal(literalStatus, process.env.EXPECT_LITERAL_VARS);

function observeArrays({ tables }) {
  const [left, right] = tables;
  assert.notEqual(left, right);
  const firstRight = right[0];
  left[0] += 1;
  assert.equal(right[0], firstRight);
  return [left, right].map((array) => ({ length: array.length, sha256: digest(JSON.stringify(array)) }));
}
const arrays = "export const tables = [" + first.name + ", " + second.name + "];";
await trial("array-vars-kept", [0, 1], arrays, kept, observeArrays);
await trial("array-vars-advance-second", [0, 1], arrays, advance, observeArrays);

// Reversing an eager read of a hoisted var changes the value despite normal
// completion in both orders. Check the unsafe request as well as the Node oracle.
const primitive = declarations[2];
const read = "var observed = " + primitive.name + ";";
const alias = (initializers) =>
  import("data:text/javascript," + encodeURIComponent(initializers + "\nexport { observed };\n"));
const before = await alias(primitive.text + "\n" + read);
const after = await alias(read + "\n" + primitive.text);
assert.equal(before.observed, primitive.init.value);
assert.equal(after.observed, undefined);
assert.equal(
  await trial(
    "unsafe-eager-read",
    [2],
    read + "\nexport const result = observed;",
    {
      entry: "m1.mjs",
      modules: { "observer.mjs": ["#1"], "m0.mjs": ["#0"], "m1.mjs": ["#2"] },
      order: ["observer.mjs", "m0.mjs"],
      sequential: true,
    },
    (m) => typeof m.result
  ),
  "refused",
  "a read of an uninitialized var must not move ahead of its initializer"
);
writeFileSync(path.join(artifacts, "results.json"), JSON.stringify(results, null, 2));
