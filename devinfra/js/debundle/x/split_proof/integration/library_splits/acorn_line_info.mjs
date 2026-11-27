import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

const source = process.argv[2];
const digest = (text) => createHash("sha256").update(text).digest("hex");
const parse = (code) => parseModule(code, acorn.parse, eslintScope);
const original = parse(readFileSync(source, "utf8"));
const sourceSha256 = digest(original.code);
assert.equal(sourceSha256, "e2478dfecff35578381298798b067b66c0424057a99c4c6ddddc6f6b2fb45eee");

const units = topLevelStatements(original.ast);
assert.equal(units.length, 447);
const scope = original.scopes.scopes.find((s) => s.type === "module");
const ordinal = ({ start, end }) => units.findIndex((u) => u.start <= start && end <= u.end);
const a = 22; // isNewLine
const b = 23; // nextLineBreak; called by getLineInfo
const effects = [36]; // Position.prototype.offset; part of the returned object's interface
const publicNames = ["getLineInfo", "isNewLine"];
const exportRow = units.at(-1).node;
assert.equal(exportRow.type, "ExportNamedDeclaration");
assert.equal(exportRow.declaration, null);
const exports = exportRow.specifiers.filter((sp) => publicNames.includes(sp.exported.name));
assert.deepEqual(exports.map((sp) => sp.exported.name).sort(), publicNames);
const readSites = (v) => new Set(v.references.filter((ref) => ref.isRead()).map((ref) => ordinal(ref.identifier)));
assert(readSites(scope.set.get("isNewLine")).has(b));
assert(readSites(scope.set.get("nextLineBreak")).has(38));

const dependencies = units.map(() => new Set());
for (const variable of scope.variables) {
  const definitions = new Set(variable.identifiers.map(ordinal));
  for (const ref of variable.references) if (ref.isWrite()) definitions.add(ordinal(ref.identifier));
  for (const read of readSites(variable)) {
    for (const definition of definitions) dependencies[read].add(definition);
  }
}
const retained = new Set([
  ...exports.flatMap((sp) => scope.set.get(sp.local.name).identifiers.map(ordinal)),
  ...effects,
]);
for (const id of retained) for (const dep of dependencies[id]) retained.add(dep);
const ordinals = [...retained].sort((x, y) => x - y);
assert.deepEqual(ordinals, [22, 23, 35, 36, 38]);
const text = new Map(
  ordinals.map((id) => {
    const { node, start, end } = units[id];
    assert.equal(start, node.start, "expected a whole statement");
    assert.equal(end, node.end);
    return [id, original.code.slice(start, end)];
  })
);
const publication = "export { " + exports.map((sp) => original.code.slice(sp.start, sp.end)).join(", ") + " };\n";
const code = [...text.values()].join("\n") + "\n" + publication;
assert.equal(digest(code), "c3e458d7a67d6a8b781b7d3ff8052bc135db59b36eeac620c470f9a01b2c83ab");
const projection = parse(code);
assert.equal(topLevelStatements(projection.ast).length, ordinals.length + 1);
assert.deepEqual(projection.scopes.globalScope.through, [], "no ambient binding in the projection");

const tmp = process.env.TEST_TMPDIR;
const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const input = path.join(artifacts, "line-info.mjs");
const entry = "entry.mjs";
writeFileSync(input, code);
const readModule = (file) => import(pathToFileURL(file).href);
const full = await readModule(source);
const probes = [
  ["plain", 4],
  ["a\r\nb", 2],
  ["a\r\nb", 3],
  ["a\u2028b\u2029c", 5],
  ["\u{10400}\nx", 3],
];
function observe({ getLineInfo, isNewLine }) {
  const positions = probes.map(([str, index]) => getLineInfo(str, index));
  const codeUnitReads = [];
  const custom = {
    charCodeAt(i) {
      assert.equal(this, custom);
      codeUnitReads.push(i);
      return "a\r\nb".charCodeAt(i);
    },
  };
  const position = getLineInfo(custom, 3);
  return {
    codeUnitReads,
    customPosition: [position.line, position.column],
    direct: [10, 13, 32, 8232, 8233].map((n) => isNewLine(n)),
    positions: positions.map((value) => {
      const moved = value.offset(2);
      assert.equal(Object.getPrototypeOf(moved), Object.getPrototypeOf(value));
      assert.notEqual(value, moved);
      return [value.line, value.column, moved.line, moved.column];
    }),
  };
}
const expected = observe(full);
assert.deepEqual(expected.codeUnitReads, [0, 1, 2]);
assert.deepEqual(expected.customPosition, [2, 0]);
assert.deepEqual(expected.positions, [
  [1, 4, 1, 6],
  [2, 0, 2, 2],
  [2, 0, 2, 2],
  [3, 1, 3, 3],
  [2, 0, 2, 2],
]);
assert.deepEqual(Object.keys(await readModule(input)), publicNames);
assert.deepEqual(observe(await readModule(input)), expected);

// Query source classification independently. The linked test runner uses the
// empty profile; this request does not run a constructor or publish files.
const [classification] = JSON.parse(
  execFileSync(process.env.LINKED_INPUT_CHECKER, {
    input:
      JSON.stringify([
        {
          kind: "singletonSourceShape",
          input: {
            source: dumpParsed(projection, { includeOpaqueCaptures: true }),
            entry,
          },
        },
      ]) + "\n",
    encoding: "utf8",
  })
);
assert(classification.output, JSON.stringify(classification));

const newline = "newline.mjs";
const scanner = "scanner.mjs";
const ids = [...ordinals, units.length - 1];
const names = new Map([
  [a, "isNewLine"],
  [b, "nextLineBreak"],
]);
async function nodeLayout(dir, owners, order, miswired = false) {
  // A handwritten Node control for the tested edge. Only the import/export
  // wiring is new; all original statements and the public row are copied.
  const ownerA = owners.get(a);
  const ownerB = owners.get(b);
  const emitImport = (name, file) => "import { " + name + " } from './" + file + "';\n";
  mkdirSync(dir);
  for (const file of [...order, entry]) {
    let prefix = "";
    if (file === ownerB && ownerA !== ownerB) {
      prefix += emitImport(names.get(a), miswired ? "wrong.mjs" : ownerA);
    }
    if (file === entry) {
      if (ownerA !== entry && ownerB !== entry) prefix += emitImport(names.get(a), ownerA);
      if (ownerB !== entry) prefix += emitImport(names.get(b), ownerB);
    }
    const body = ordinals
      .filter((id) => owners.get(id) === file)
      .map((id) => text.get(id))
      .join("\n");
    const declared = [a, b].filter((id) => owners.get(id) === file).map((id) => names.get(id));
    const suffix = file === entry ? publication : "export { " + declared.join(", ") + " };\n";
    writeFileSync(path.join(dir, file), prefix + body + "\n" + suffix);
  }
  if (miswired) writeFileSync(path.join(dir, "wrong.mjs"), "export function isNewLine() { return false; }\n");
  return readModule(path.join(dir, entry));
}

function checkUnionGraph(out) {
  const read = (file) => parse(readFileSync(path.join(out, file), "utf8"));
  const imports = ({ ast }) =>
    ast.body
      .filter((node) => node.type === "ImportDeclaration")
      .map(({ source, specifiers }) => [
        source.value,
        specifiers.map(({ imported, local }) => [imported.name, local.name]),
      ]);
  const retainedSource = ({ ast, code }) =>
    topLevelStatements(ast)
      .filter(({ node }) => !["ImportDeclaration", "ExportNamedDeclaration"].includes(node.type))
      .map(({ start, end }) => code.slice(start, end));
  const helper = read(newline);
  const reader = read(scanner);
  const main = read(entry);
  assert.deepEqual(imports(helper), []);
  assert.deepEqual(imports(reader), [["./" + newline, [["isNewLine", "isNewLine"]]]]);
  assert.deepEqual(imports(main), [
    ["./" + newline, [["isNewLine", "isNewLine"]]],
    ["./" + scanner, [["nextLineBreak", "nextLineBreak"]]],
  ]);
  assert.deepEqual(retainedSource(helper), [text.get(a)]);
  assert.deepEqual(retainedSource(reader), [text.get(b)]);
  assert.deepEqual(
    retainedSource(main),
    ordinals.slice(2).map((id) => text.get(id))
  );
  const publicRow = main.ast.body.find((node) => node.type === "ExportNamedDeclaration");
  assert.deepEqual(
    publicRow.specifiers.map(({ exported, local }) => [exported.name, local.name]),
    exports.map(({ exported, local }) => [exported.name, local.name])
  );
}

// Replace just the callee identifier with the scanner's own name. All
// statements, declarations and imports stay in place. Use a separate tree
// so the scanner cannot be loaded from Node's cache of the valid output.
async function checkMiswiredArtifact(request, owner) {
  const tamperedOut = request.out + "-wrong-scanner";
  cpSync(request.out, tamperedOut, { recursive: true });
  const file = path.join(tamperedOut, owner);
  const originalBytes = readFileSync(file, "utf8");
  const parsed = parse(originalBytes);
  const module = parsed.scopes.scopes.find((scope) => scope.type === "module");
  const scanner = topLevelStatements(parsed.ast).find(({ node }) => node.id?.name === names.get(b));
  const scannerReads = module.set.get(names.get(a)).references.filter(({ identifier }) => {
    return scanner.start <= identifier.start && identifier.end <= scanner.end;
  });
  assert.equal(scannerReads.length, 1);
  const [{ identifier }] = scannerReads;
  const tampered = originalBytes.slice(0, identifier.start) + names.get(b) + originalBytes.slice(identifier.end);
  const statementTypes = ({ ast }) => ast.body.map(({ type }) => type);
  assert.deepEqual(statementTypes(parse(tampered)), statementTypes(parsed));
  writeFileSync(file, tampered);
  const wrong = await readModule(path.join(tamperedOut, entry));
  assert.deepEqual(Object.keys(wrong), publicNames);
  const actual = observe(wrong);
  assert.deepEqual(actual.direct, expected.direct);
  assert.notDeepEqual(actual.positions[2], expected.positions[2]);
  const reread = split({ ...request, out: tamperedOut, checkOutput: true });
  assert.equal(reread.status, "refused", reread.log);
  assert(reread.reason.includes(owner), reread.log);
}

// Change only the entry export's local cell. The scanner still asks for
// isNewLine and the two files still form the same cycle.
function checkScannerCycle(request) {
  const imports = (file) =>
    parse(readFileSync(path.join(request.out, file), "utf8"))
      .ast.body.filter((node) => node.type === "ImportDeclaration")
      .map((node) => [node.source.value, node.specifiers.map((sp) => [sp.imported.name, sp.local.name])]);
  assert.deepEqual(imports(scanner), [["./entry.mjs", [["isNewLine", "isNewLine"]]]]);
  assert.deepEqual(imports(entry), [["./scanner.mjs", [["nextLineBreak", "nextLineBreak"]]]]);

  const wrong = request.out + "-wrong-cell";
  cpSync(request.out, wrong, { recursive: true });
  const file = path.join(wrong, entry);
  const originalBytes = readFileSync(file, "utf8");
  const exportSpec = parse(originalBytes)
    .ast.body.find((node) => node.type === "ExportNamedDeclaration")
    .specifiers.find((sp) => sp.exported.name === "isNewLine");
  const changed =
    originalBytes.slice(0, exportSpec.start) + "getLineInfo as isNewLine" + originalBytes.slice(exportSpec.end);
  writeFileSync(file, changed);
  const wrongResult = split({ ...request, out: wrong, checkOutput: true });
  assert.equal(wrongResult.status, "refused", wrongResult.log);
  assert.match(wrongResult.reason, /entry\.mjs differs from its generated ESTree/);
  cpSync(wrong, path.join(artifacts, "B-wrong-cell"), { recursive: true });
  return wrongResult.verdict;
}

function checkEagerCycle(request, modules, order) {
  const call = 'getLineInfo("a\\nb", 2);\n';
  const eagerSource = path.join(artifacts, "B-eager-source.mjs");
  writeFileSync(eagerSource, code + call);
  const eagerSpec = path.join(artifacts, "B-eager-spec.json");
  writeFileSync(
    eagerSpec,
    JSON.stringify(
      { entry, modules: { ...modules, [scanner]: [...modules[scanner], "#6"] }, order, sequential: true },
      null,
      2
    )
  );

  // Use real emitted files as the basis for the eager control.
  const eagerDir = path.join(artifacts, "B-eager-node");
  cpSync(request.out, eagerDir, { recursive: true });
  const file = path.join(eagerDir, scanner);
  writeFileSync(file, 'import { getLineInfo } from "./entry.mjs";\n' + readFileSync(file, "utf8") + "\n" + call);
  const run = (main) => spawnSync(process.execPath, [main], { encoding: "utf8" });
  const flat = run(eagerSource);
  const early = run(path.join(eagerDir, entry));
  assert.ifError(flat.error);
  assert.ifError(early.error);
  assert.equal(flat.status, 0, flat.stderr);
  assert.equal(early.status, 1, early.stderr);
  assert.match(early.stderr, /TypeError: Position is not a constructor/);

  const refusals = [false, true].map((checkOutput) => {
    // split snapshots the previous certified B tree and checks that every
    // byte and file timestamp is unchanged on refusal.
    const result = split({ ...request, source: eagerSource, spec: eagerSpec, allowExistingOut: true, checkOutput });
    assert.equal(result.status, "refused", result.log);
    assert.equal(result.scope, "indexed-hoisted-swap", result.log);
    assert.equal(result.reasonCode, "no_constructed_rule", result.log);
    assert.match(
      result.reason,
      /authored plan (failed validity checks|changes ordinals beyond the adjacent declaration swap)/
    );
    return result.verdict;
  });
  return { flatNodeStatus: flat.status, eagerNodeStatus: early.status, refusals };
}

const results = [];
for (const [name, moved] of [
  ["A", [a]],
  ["B", [b]],
  ["A+B", [a, b]],
]) {
  const owners = new Map(ids.map((i) => [i, moved.includes(i) ? (i === a ? newline : scanner) : entry]));
  const order = moved.map((i) => owners.get(i));
  const modules = Object.fromEntries(
    [...order, entry].map((owner) => [
      owner,
      ids.flatMap((id, index) => (owners.get(id) === owner ? ["#" + index] : [])),
    ])
  );
  const control = await nodeLayout(path.join(tmp, "node-" + name), owners, order);
  assert.deepEqual(Object.keys(control), publicNames);
  assert.deepEqual(observe(control), expected);
  if (name === "A+B") {
    const wrong = await nodeLayout(path.join(tmp, "node-miswired"), owners, order, true);
    assert.deepEqual(Object.keys(wrong), publicNames);
    assert.deepEqual(observe(wrong).direct, expected.direct);
    assert.deepEqual(observe(wrong).positions[2], [1, 3, 1, 5]);
    assert.notDeepEqual(observe(wrong), expected);
  }
  const spec = path.join(artifacts, name + "-spec.json");
  writeFileSync(spec, JSON.stringify({ entry, modules, order, sequential: true }, null, 2));
  const request = { source: input, spec, out: path.join(tmp, "cli-" + name), require: "all-outcomes" };
  const result = split(request);
  assert.equal(result.status, "certified", result.log);
  assert.equal(result.scope, name === "B" ? "indexed-hoisted-swap" : "indexed-exported-function", result.log);
  assert.deepEqual(result.files, [entry, ...order.toSorted()]);
  assert.equal(result.verdict.strategy, "compiled-constructor", result.log);
  assert.equal(result.guarantee, "all-outcomes", result.log);
  assert.deepEqual(split({ ...request, checkOutput: true }).verdict, result.verdict);
  const actual = await readModule(path.join(request.out, entry));
  assert.deepEqual(Object.keys(actual), publicNames);
  assert.deepEqual(observe(actual), expected);
  let controls;
  if (name === "B") {
    cpSync(request.out, path.join(artifacts, "B-emitted"), { recursive: true });
    controls = { wrongCell: checkScannerCycle(request), eager: checkEagerCycle(request, modules, order) };
  } else {
    if (name === "A+B") {
      checkUnionGraph(request.out);
      const swappedSpec = path.join(tmp, "union-swapped-priority.json");
      writeFileSync(swappedSpec, JSON.stringify({ entry, modules, order: [...order].reverse(), sequential: true }));
      const swapped = split({ ...request, spec: swappedSpec, allowExistingOut: true });
      assert.equal(swapped.status, "refused", swapped.log);
      assert.equal(swapped.reasonCode, "no_constructed_rule", swapped.log);
    }
    await checkMiswiredArtifact(request, name === "A" ? entry : scanner);
  }
  assert.deepEqual(split({ ...request, checkOutput: true }).verdict, result.verdict);
  const row = {
    name,
    sourceSha256,
    projectionSha256: digest(code),
    ids,
    order,
    modules,
    sourceDecoded: true,
    verdict: result.verdict,
    files: result.files,
    controls,
  };
  results.push(row);
  console.log(JSON.stringify(row));
}
// A modeled outer function must not hide a deferred body inside the entry.
const deferredSource = path.join(tmp, "deferred.mjs");
writeFileSync(
  deferredSource,
  "function predicate(value) { return value === 10; }\n" +
    "function retained() { return async function () { return 1; }; }\n" +
    "export { predicate, retained };\n"
);
const deferredSpec = path.join(tmp, "deferred-spec.json");
writeFileSync(
  deferredSpec,
  JSON.stringify({ entry, modules: { [newline]: ["#0"], [entry]: ["#1", "#2"] }, order: [newline], sequential: true })
);
const deferred = split({
  source: deferredSource,
  spec: deferredSpec,
  out: path.join(tmp, "deferred-out"),
  require: "all-outcomes",
});
assert.equal(deferred.status, "refused", deferred.log);
assert.match(deferred.reason, /entry unit 1: expected concrete retained code/);

writeFileSync(path.join(artifacts, "results.json"), JSON.stringify({ expected, results }, null, 2));
