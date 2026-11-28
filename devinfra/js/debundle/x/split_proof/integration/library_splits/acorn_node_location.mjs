import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { cpSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
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
const moduleScope = original.scopes.scopes.find((scope) => scope.type === "module");
const ordinal = ({ start, end }) => units.findIndex((unit) => unit.start <= start && end <= unit.end);
const bindingOrdinal = (name) => {
  const [binding] = moduleScope.set.get(name).identifiers;
  return ordinal(binding);
};
const nodeOrdinal = bindingOrdinal("Node");
assert.equal(nodeOrdinal, 269);
const publicNames = ["Node", "getLineInfo", "isNewLine"];
const exportOrdinal = units.length - 1;
const exportRow = units[exportOrdinal].node;
assert.equal(exportRow.type, "ExportNamedDeclaration");
assert.equal(exportRow.declaration, null);
const exported = exportRow.specifiers.filter((sp) => publicNames.includes(sp.exported.name));
assert.deepEqual(exported.map((sp) => sp.exported.name).sort(), publicNames);

// Include every definition and write to a free module cell of a retained
// statement. Also keep Position.prototype.offset, which the returned object exposes.
const dependencies = units.map(() => new Set());
for (const variable of moduleScope.variables) {
  const definitions = new Set(variable.identifiers.map(ordinal));
  for (const ref of variable.references) if (ref.isWrite()) definitions.add(ordinal(ref.identifier));
  for (const ref of variable.references) {
    if (ref.isRead()) for (const definition of definitions) dependencies[ordinal(ref.identifier)].add(definition);
  }
}
const retained = new Set([...exported.map((sp) => bindingOrdinal(sp.local.name)), 36]);
for (const id of retained) for (const dependency of dependencies[id]) retained.add(dependency);
const ids = [...retained].sort((a, b) => a - b);
assert.deepEqual(ids, [22, 23, 35, 36, 37, 38, nodeOrdinal]);
assert(dependencies[nodeOrdinal].has(37)); // Node constructs SourceLocation.
assert(dependencies[38].has(35)); // getLineInfo constructs Position.
const statement = (id) => {
  const { node, start, end } = units[id];
  assert.equal(start, node.start);
  assert.equal(end, node.end);
  return original.code.slice(start, end);
};
const codeFor = (ordinals) => ordinals.map(statement).join("\n") + "\n";
const publication = "export { " + exported.map((sp) => original.code.slice(sp.start, sp.end)).join(", ") + " };\n";
const code = codeFor(ids) + publication;
assert.deepEqual(parse(code).scopes.globalScope.through, [], "expected no ambient binding reads");

const tmp = process.env.TEST_TMPDIR;
const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const input = path.join(artifacts, "node-location.mjs");
writeFileSync(input, code);
const readModule = (file) => import(pathToFileURL(file).href);
const full = await readModule(source);
const programText = 'const first = 1;\r\nlet second = "𐐀";\u2028// tail';
const options = { ecmaVersion: "latest", locations: true, ranges: true, sourceFile: "input.js" };
const parsed = full.parse(programText, options).body[1];
const start = parsed.start;
const positionFields = (pos) => [pos.line, pos.column];

function observe(api) {
  const location = api.getLineInfo(programText, start);
  const token = new api.Node(
    { options: { ...options, directSourceFile: "tokens.js" }, sourceFile: "input.js" },
    start,
    location
  );
  assert.deepEqual(positionFields(location), positionFields(parsed.loc.start));
  assert.equal(token.loc.start, location);
  assert.equal(token.loc.end, undefined);
  const peer = new api.Node({ options, sourceFile: "input.js" }, start, location);
  assert.deepEqual(token.range, peer.range);
  assert.notEqual(token.range, peer.range);
  token.range[1] = parsed.end;
  assert.deepEqual(peer.range, [start, 0]);
  assert.deepEqual(new api.Node({ options, sourceFile: "input.js" }, start, location).range, [start, 0]);
  assert.equal(token.range[1], parsed.end);
  const shifted = token.loc.start.offset(4);
  assert.notEqual(shifted, location);
  assert.equal(Object.getPrototypeOf(shifted), Object.getPrototypeOf(location));
  const bare = new api.Node({ options: { locations: false, ranges: false }, sourceFile: null }, start, location);
  assert(!Object.hasOwn(bare, "loc") && !Object.hasOwn(bare, "range"));
  return {
    position: positionFields(token.loc.start),
    shifted: positionFields(shifted),
    type: token.type,
    range: peer.range,
    sourceFile: token.sourceFile,
    locationSource: token.loc.source,
    newline: api.isNewLine(13),
  };
}
const expected = observe(full);
assert.deepEqual(expected.position, [2, 0]);
assert.deepEqual(expected.range, [18, 0]);
const projection = await readModule(input);
assert.deepEqual(Object.keys(projection), publicNames);
assert.deepEqual(observe(projection), expected);

const entry = "entry.mjs";
const newline = "newline.mjs";
const scanner = "scanner.mjs";
const locations = "locations.mjs";
const rows = [...ids, exportOrdinal];
const selectors = (ordinals) => ordinals.map((id) => "#" + rows.indexOf(id));
function instances(api) {
  const position = api.getLineInfo(programText, start);
  const token = new api.Node({ options, sourceFile: "input.js" }, start, position);
  assert.equal(token.loc.start, position);
  return { position, location: token.loc };
}

async function checkExtractedArtifacts(request, api, modules, order) {
  const constructors = await readModule(path.join(request.out, locations));
  const { position, location } = instances(api);
  assert(position instanceof constructors.Position);
  assert(position.offset(4) instanceof constructors.Position);
  assert(location instanceof constructors.SourceLocation);

  const entryCode = readFileSync(path.join(request.out, entry), "utf8");
  const locationCode = readFileSync(path.join(request.out, locations), "utf8");
  const locationAst = parse(locationCode).ast;
  const locationImports = locationAst.body.filter((node) => node.type === "ImportDeclaration");
  assert.deepEqual(locationImports, []);
  const importAliases = parse(entryCode)
    .ast.body.filter((node) => node.type === "ImportDeclaration")
    .map(({ source, specifiers }) => [
      source.value,
      specifiers.map(({ imported, local }) => [imported.name, local.name]),
    ]);
  assert.deepEqual(importAliases, [
    ["./" + newline, [["isNewLine", "isNewLine"]]],
    ["./" + scanner, [["nextLineBreak", "nextLineBreak"]]],
    [
      "./" + locations,
      [
        ["Position", "Position"],
        ["SourceLocation", "SourceLocation"],
      ],
    ],
  ]);
  const [ctor, prototype, secondCtor] = locationAst.body;
  assert.deepEqual(
    [ctor, prototype, secondCtor].map(({ start, end }) => locationCode.slice(start, end)),
    [35, 36, 37].map(statement)
  );

  // Rebind only Node's imported constructor. Its fields and the public API
  // still agree, but the object no longer has the original prototype.
  const wrongCell = request.out + "-wrong-cell";
  cpSync(request.out, wrongCell, { recursive: true });
  const { id } = secondCtor.declarations[0];
  const twin =
    locationCode.slice(secondCtor.start, id.start) + "TwinLocation" + locationCode.slice(id.end, secondCtor.end);
  writeFileSync(path.join(wrongCell, locations), locationCode + "\n" + twin + "\nexport { TwinLocation };\n");
  const importDecl = parse(entryCode).ast.body.find((node) => node.source?.value === "./" + locations);
  const alias = importDecl.specifiers.find(({ imported }) => imported.name === "SourceLocation");
  writeFileSync(
    path.join(wrongCell, entry),
    entryCode.slice(0, alias.start) + "TwinLocation as SourceLocation" + entryCode.slice(alias.end)
  );
  const wrongApi = await readModule(path.join(wrongCell, entry));
  assert.deepEqual(Object.keys(wrongApi), publicNames);
  assert.deepEqual(observe(wrongApi), expected);
  const wrongConstructors = await readModule(path.join(wrongCell, locations));
  const wrong = instances(wrongApi).location;
  assert(wrong instanceof wrongConstructors.TwinLocation);
  assert(!(wrong instanceof wrongConstructors.SourceLocation));
  assert.equal(split({ ...request, out: wrongCell, checkOutput: true }).status, "refused");

  // The prototype installation is an eager write, not closure metadata.
  const earlyWrite = request.out + "-early-prototype";
  cpSync(request.out, earlyWrite, { recursive: true });
  writeFileSync(
    path.join(earlyWrite, locations),
    locationCode.slice(prototype.start, prototype.end) +
      "\n" +
      locationCode.slice(ctor.start, ctor.end) +
      locationCode.slice(prototype.end)
  );
  await assert.rejects(readModule(path.join(earlyWrite, entry)), TypeError);
  assert.equal(split({ ...request, out: earlyWrite, checkOutput: true }).status, "refused");

  const swapped = path.join(tmp, "locations-before-scanner.json");
  writeFileSync(swapped, JSON.stringify({ entry, modules, order: [order[0], order[2], order[1]], sequential: true }));
  const wrongOrder = split({ ...request, spec: swapped, allowExistingOut: true });
  assert.equal(wrongOrder.status, "refused", wrongOrder.log);
  assert.equal(wrongOrder.reasonCode, "no_constructed_rule");
  assert.equal(split({ ...request, checkOutput: true }).status, "certified");
}

const results = [];
for (const [name, locationRows] of [
  ["node-kept", []],
  ["locations-extracted", [35, 36, 37]],
  ["locutil-grouped", [35, 36, 37, 38]],
]) {
  const moveLocations = locationRows.length > 0;
  const moveLineInfo = locationRows.includes(38);
  const order = [newline, scanner, ...(moveLocations ? [locations] : [])];
  const owners = {
    [newline]: [22],
    [scanner]: [23],
    ...(moveLocations ? { [locations]: locationRows } : {}),
    [entry]: [35, 36, 37, 38, nodeOrdinal, exportOrdinal].filter((id) => !locationRows.includes(id)),
  };
  const modules = Object.fromEntries(Object.entries(owners).map(([file, ordinals]) => [file, selectors(ordinals)]));

  // Independent Node layout of the authored placement. Glue is the only new
  // code; the original constructors, scanner, and public row are copied above.
  const control = path.join(tmp, name + "-node");
  mkdirSync(control);
  writeFileSync(path.join(control, newline), codeFor([22]) + "export { isNewLine };\n");
  writeFileSync(
    path.join(control, scanner),
    'import { isNewLine } from "./newline.mjs";\n' + codeFor([23]) + "export { nextLineBreak };\n"
  );
  if (moveLocations) {
    const scannerImport = moveLineInfo ? 'import { nextLineBreak } from "./scanner.mjs";\n' : "";
    const groupExports = moveLineInfo ? "getLineInfo, SourceLocation, Position" : "Position, SourceLocation";
    writeFileSync(
      path.join(control, locations),
      scannerImport + codeFor(locationRows) + `export { ${groupExports} };\n`
    );
  }
  writeFileSync(
    path.join(control, entry),
    'import { isNewLine } from "./newline.mjs";\nimport { nextLineBreak } from "./scanner.mjs";\n' +
      (moveLocations
        ? `import { ${moveLineInfo ? "getLineInfo" : "Position"}, SourceLocation } from "./locations.mjs";\n`
        : "") +
      codeFor(owners[entry].filter((id) => id !== exportOrdinal)) +
      publication
  );
  const node = await readModule(path.join(control, entry));
  assert.deepEqual(Object.keys(node), publicNames);
  assert.deepEqual(observe(node), expected);
  if (moveLocations) {
    const types = await readModule(path.join(control, locations));
    const location = node.getLineInfo(programText, start);
    const token = new node.Node({ options, sourceFile: "input.js" }, start, location);
    assert(location instanceof types.Position);
    assert(token.loc instanceof types.SourceLocation);
    assert.equal(token.loc.start, location);
  }

  const spec = path.join(artifacts, name + "-spec.json");
  writeFileSync(spec, JSON.stringify({ entry, modules, order, sequential: true }, null, 2));
  const request = { source: input, spec, out: path.join(tmp, name + "-cli"), require: "all-outcomes" };
  const result = split(request);
  if (moveLineInfo) assert.equal(result.status, "certified", result.log);
  if (result.status === "certified") {
    assert.equal(result.guarantee, "all-outcomes", result.log);
    assert.deepEqual(split({ ...request, checkOutput: true }).verdict, result.verdict);
    const emitted = await readModule(path.join(request.out, entry));
    assert.deepEqual(Object.keys(emitted), publicNames);
    assert.deepEqual(observe(emitted), expected);
    assert.deepEqual(result.files, moveLocations ? [entry, locations, newline, scanner] : [entry, newline, scanner]);
    if (moveLocations && !moveLineInfo) await checkExtractedArtifacts(request, emitted, modules, order);
    if (moveLineInfo) {
      const groupFile = path.join(request.out, locations);
      const group = readFileSync(groupFile, "utf8");
      const groupModule = await readModule(groupFile);
      const { position, location } = instances(emitted);
      assert.equal(emitted.getLineInfo, groupModule.getLineInfo);
      assert(location instanceof groupModule.SourceLocation);
      assert.equal(Object.getPrototypeOf(position.offset(4)), Object.getPrototypeOf(position));
      const parsedGroup = parse(group);
      const originalSequence = parsedGroup.ast.body.filter(
        (row) => row.type !== "ImportDeclaration" && row.type !== "ExportNamedDeclaration"
      );
      assert.deepEqual(
        originalSequence.map(({ start, end }) => group.slice(start, end)),
        locationRows.map(statement)
      );
      const [reader] = parsedGroup.ast.body.filter((row) => row.type === "ImportDeclaration");
      assert.equal(reader.source.value, "./scanner.mjs");
      assert.deepEqual(
        reader.specifiers.map(({ imported, local }) => [imported.name, local.name]),
        [["nextLineBreak", "nextLineBreak"]]
      );
      // Same group, but link its scanner name to a different function.
      const wrong = request.out + "-wrong-locutil-reader";
      cpSync(request.out, wrong, { recursive: true });
      writeFileSync(
        path.join(wrong, scanner),
        readFileSync(path.join(wrong, scanner), "utf8") + "\nexport function noBreak() { return -1; }\n"
      );
      const replacement = 'import { noBreak as nextLineBreak } from "./scanner.mjs";';
      writeFileSync(path.join(wrong, locations), group.slice(0, reader.start) + replacement + group.slice(reader.end));
      const changed = await readModule(path.join(wrong, entry));
      assert.deepEqual(Object.keys(changed), publicNames);
      assert.notDeepEqual(positionFields(changed.getLineInfo(programText, start)), expected.position);
      assert.equal(split({ ...request, out: wrong, checkOutput: true }).status, "refused");
    }
  } else {
    assert.equal(result.reasonCode, "no_constructed_rule", result.log);
  }
  const row = {
    name,
    sourceSha256,
    sourceBytes: Buffer.byteLength(original.code),
    projectionSha256: digest(code),
    projectionBytes: Buffer.byteLength(code),
    originalOrdinals: rows,
    order,
    owners,
    modules,
    verdict: result.verdict,
    files: result.files,
    node: expected,
  };
  results.push(row);
  console.log(JSON.stringify(row));
}
writeFileSync(path.join(artifacts, "results.json"), JSON.stringify(results, null, 2));

if (process.env.EXPECT_NODE_LOCATION) {
  for (const row of results) assert.equal(row.verdict.status, process.env.EXPECT_NODE_LOCATION, row.name);
}

// Spread invokes the iterator protocol, outside this dense array fragment.
const spreadSource = path.join(tmp, "spread.mjs");
const spreadSpec = path.join(tmp, "spread-spec.json");
writeFileSync(
  spreadSource,
  "function predicate(n) { return n === 10; }\nfunction copy(items) { return [...items]; }\nexport { predicate, copy };\n"
);
writeFileSync(
  spreadSpec,
  JSON.stringify({ entry, modules: { [newline]: ["#0"], [entry]: ["#1", "#2"] }, order: [newline], sequential: true })
);
const spread = split({
  source: spreadSource,
  spec: spreadSpec,
  out: path.join(tmp, "spread-out"),
  require: "all-outcomes",
});
assert.equal(spread.reasonCode, "no_constructed_rule", spread.log);
assert.match(spread.reason, /entry unit 1: expected concrete retained code/);
