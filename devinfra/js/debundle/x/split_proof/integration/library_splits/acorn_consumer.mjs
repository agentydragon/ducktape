import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

const source = process.argv[2];
const digest = (text) => createHash("sha256").update(text).digest("hex");
const original = parseModule(readFileSync(source, "utf8"), acorn.parse, eslintScope);
const sourceSha256 = digest(original.code);
assert.equal(sourceSha256, "e2478dfecff35578381298798b067b66c0424057a99c4c6ddddc6f6b2fb45eee");
const units = topLevelStatements(original.ast);
assert.equal(units.length, 447);
const scope = original.scopes.scopes.find((s) => s.type === "module");
const ordinal = ({ start, end }) => {
  const i = units.findIndex((u) => u.start <= start && end <= u.end);
  assert(i >= 0);
  return i;
};
const deps = units.map(() => new Set());
for (const v of scope.variables) {
  // Preserve every declaration and write site of a binding read by a kept unit.
  const definitions = new Set(v.identifiers.map(ordinal));
  for (const ref of v.references) if (ref.isWrite()) definitions.add(ordinal(ref.identifier));
  for (const ref of v.references) {
    for (const origin of definitions) deps[ordinal(ref.identifier)].add(origin);
  }
}
const entry = "m1.mjs";
const full = await import(pathToFileURL(source).href);
const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const results = [];

function sourceProbe(projection, spec) {
  const input = {
    source: dumpParsed(projection, { includeOpaqueCaptures: true }),
    sourceText: projection.code,
    spec,
    entry,
  };
  const [classification, indexedInline] = JSON.parse(
    execFileSync(process.env.LINKED_INPUT_CHECKER, {
      input: JSON.stringify(["singletonSourceShape", "indexedInlinePlan"].map((kind) => ({ kind, input }))) + "\n",
      encoding: "utf8",
    })
  );
  return {
    classification: classification.error ?? "decoded",
    indexedInline: indexedInline.error ?? "constructed",
  };
}

async function trial(name, names, observe, expectedOrdinals) {
  const metadata = units.at(-1).node;
  assert.equal(metadata.type, "ExportNamedDeclaration");
  assert.equal(metadata.declaration, null);
  const exports = metadata.specifiers.filter((sp) => names.includes(sp.exported.name));
  assert.deepEqual(exports.map((sp) => sp.exported.name).sort(), [...names].sort());
  const wanted = new Set(exports.map((sp) => ordinal(scope.set.get(sp.local.name).identifiers[0])));
  for (const i of wanted) for (const dependency of deps[i]) wanted.add(dependency);
  const ordinals = [...wanted].sort((a, b) => a - b);
  if (expectedOrdinals) assert.deepEqual(ordinals, expectedOrdinals);
  assert(
    ordinals.every((i) => i < Math.floor(units.length / 2)),
    "the declarations must retain their halves owner"
  );
  const body = ordinals.map((i) => {
    const { start, end, node } = units[i];
    assert.equal(node.start, start, "expected a whole source statement");
    assert.equal(node.end, end);
    return original.code.slice(start, end);
  });
  // Retain the original export specifiers, in source order. Drop only the
  // unselected public names, so this entry exposes the original functions.
  body.push("export { " + exports.map((sp) => original.code.slice(sp.start, sp.end)).join(", ") + " };");
  const code = body.join("\n") + "\n";
  const projected = parseModule(code, acorn.parse, eslintScope);
  assert.equal(topLevelStatements(projected.ast).length, body.length);
  const projectedUnits = topLevelStatements(projected.ast);
  const globals = projected.scopes.globalScope.through.map(({ identifier, from }) => ({
    name: identifier.name,
    originalUnit:
      ordinals[projectedUnits.findIndex(({ start, end }) => start <= identifier.start && identifier.end <= end)],
    phase: from.variableScope.type === "module" ? "initialization" : "call",
  }));
  const placement = {
    entry,
    modules: {
      "m0.mjs": ordinals.map((_, i) => "#" + i),
      [entry]: ["#" + ordinals.length],
    },
    order: ["m0.mjs"],
    sequential: true,
  };
  const input = path.join(artifacts, name + ".mjs");
  const spec = path.join(artifacts, name + "-spec.json");
  writeFileSync(input, code);
  writeFileSync(spec, JSON.stringify(placement, null, 2));
  const values = observe(full);
  assert.deepEqual(observe(await import(pathToFileURL(input).href)), values);
  const out = path.join(process.env.TEST_TMPDIR, name);
  const request = { source: input, spec, out, require: "all-outcomes" };
  const result = split(request);
  if (result.status === "certified") {
    assert.deepEqual(split({ ...request, checkOutput: true }).verdict, result.verdict);
    assert.deepEqual(observe(await import(pathToFileURL(path.join(out, entry)).href)), values);
  } else {
    assert.equal(result.reasonCode, "no_constructed_rule", result.log);
    assert.equal(result.guarantee, null);
  }
  const stages = sourceProbe(projected, placement);
  const first = /^statement (\d+):/.exec(stages.classification);
  const row = {
    name,
    ordinals,
    originalExportRow: units.length - 1,
    sourceSha256,
    projectionSha256: digest(code),
    globals,
    ...stages,
    ...(first && { firstOriginalUnit: ordinals[Number(first[1])] }),
    verdict: result.verdict,
    files: result.files,
    node: values,
  };
  results.push(row);
  console.log(JSON.stringify(row));
  return row;
}

const identifiers = await trial(
  "identifiers",
  ["isIdentifierStart", "isIdentifierChar"],
  ({ isIdentifierStart, isIdentifierChar }) =>
    [
      [36, true], // $
      [48, true], // digit
      [0xb7, true], // middle dot (only a continuation)
      [0x3b1, true], // Greek BMP start
      [0x10400, true], // Deseret astral start
      [0x10400, false], // astral parsing disabled
      [0x1f680, true], // not an identifier
    ].map(([cp, astral]) => [cp, astral, isIdentifierStart(cp, astral), isIdentifierChar(cp, astral)]),
  [0, 1, 2, 3, 8, 9, 10, 11, 12]
);
assert.equal(identifiers.firstOriginalUnit, 8);
assert.match(identifiers.classification, /unsupported global RegExp/);
assert.equal(identifiers.verdict.status, "rejected");
assert.deepEqual(
  [...new Set(identifiers.globals.filter(({ name }) => name === "RegExp").map(({ phase }) => phase))],
  ["initialization"]
);
assert.deepEqual(
  [...new Set(identifiers.globals.filter(({ name }) => name === "String").map(({ phase }) => phase))],
  ["call"]
);

const lineBreak = await trial(
  "line-break-predicate",
  ["isNewLine"],
  ({ isNewLine }) => [0, 10, 13, 32, 0x2028, 0x2029].map((cp) => [cp, isNewLine(cp)]),
  [22]
);
assert.deepEqual(lineBreak.globals, []);
assert.equal(lineBreak.classification, "decoded");

writeFileSync(path.join(artifacts, "acorn-consumer.json"), JSON.stringify(results, null, 2));
