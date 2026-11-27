import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { acorn, eslintScope } from "../../translation/parser.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { walk } from "../../translation/syntax_inventory.mjs";
import { writeDeployment } from "../../translation/testing/deployment_manifest.mjs";
import { split } from "../../translation/testing/split.mjs";

const chunk = "chunk-PN7KOLDJ.js";
const bundle = process.env.EXCALIDRAW_OUTPUTS.split(" ").find((file) => file.endsWith("/bundle"));
assert(bundle);
const original = path.join(bundle, chunk);
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const code = readFileSync(original, "utf8");
const hashes = JSON.parse(readFileSync(new URL("./main_static_sha256.json", import.meta.url), "utf8"));
const originalHash = sha256(code);
assert.equal(originalHash, hashes[chunk]);

const parse = (source) => parseModule(source, acorn.parse, eslintScope);
const parsed = parse(code);
const units = topLevelStatements(parsed.ast);
assert.equal(units.length, 1787);
const moduleScope = parsed.scopes.scopes.find(({ type }) => type === "module");
const ordinal = ({ start, end }) => {
  const i = units.findIndex((u) => u.start <= start && end <= u.end);
  assert(i >= 0);
  return i;
};
const deps = units.map(() => new Set());
for (const variable of moduleScope.variables) {
  const definitions = new Set(variable.identifiers.map(ordinal));
  for (const ref of variable.references) {
    if (ref.isWrite() && ref.from.variableScope === moduleScope) definitions.add(ordinal(ref.identifier));
  }
  for (const ref of variable.references) {
    if (ref.isRead()) for (const i of definitions) deps[ordinal(ref.identifier)].add(i);
  }
}
function closure(seed) {
  const ids = new Set(seed);
  for (const i of ids) for (const dependency of deps[i]) ids.add(dependency);
  return [...ids].sort((a, b) => a - b);
}

const tmp = process.env.TEST_TMPDIR;
const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
function specFile(name, spec) {
  const file = path.join(artifacts, name + "-spec.json");
  writeFileSync(file, JSON.stringify(spec, null, 2));
  return file;
}
function probe(file, spec, kinds) {
  const ast = path.join(tmp, path.basename(file) + ".json");
  execFileSync(process.env.MODULE_IO, ["--parse", file, ast], { timeout: 90_000 });
  const input = {
    source: JSON.parse(readFileSync(ast, "utf8")),
    sourceText: readFileSync(file, "utf8"),
    entry: spec.entry,
    spec,
  };
  return JSON.parse(
    execFileSync(process.env.LINKED_INPUT_CHECKER, {
      input: JSON.stringify(kinds.map((kind) => ({ kind, input }))) + "\n",
      encoding: "utf8",
      maxBuffer: 8 * 1024 * 1024,
      timeout: 90_000,
    })
  );
}

const pair = [1337, 1338];
const scroll = "packages/excalidraw/scene/scroll.mjs";
const fullSpec = {
  entry: chunk,
  order: [scroll],
  sequential: true,
  modules: {
    [scroll]: pair.map((i) => "#" + i),
    [chunk]: units.flatMap((_, i) => (pair.includes(i) ? [] : ["#" + i])),
  },
};
const full = split({
  source: original,
  spec: specFile("full-pair", fullSpec),
  out: path.join(tmp, "full-pair"),
  require: "all-outcomes",
  timeoutMs: 90_000,
});
assert.equal(full.reasonCode, "no_constructed_rule", full.log);
assert.deepEqual(full.files, []);
const [fullSource] = probe(original, fullSpec, ["indexedInlinePlan"]);
assert.equal(fullSource.error, "statement 41: unsupported global Uint8Array");
assert(deps[1338].has(1337));
assert(deps[1338].has(1745));
assert(deps[1745].has(1699));
const wholeScroll = closure(pair);
assert.deepEqual(
  wholeScroll.filter((i) => units[i].node.type === "ImportDeclaration"),
  [0, 1, 3, 4]
);

const moved = [616, 617, 618, 621];
const consumer = 1745;
const executable = closure([consumer]);
assert.deepEqual(executable, [...moved, 1699, consumer]);
const exportId = 1786;
const row = units[exportId].node;
assert.equal(row.type, "ExportNamedDeclaration");
assert.equal(row.source, null);
const publication = row.specifiers.filter(({ local }) =>
  moduleScope.set.get(local.name).identifiers.some((id) => executable.includes(ordinal(id)))
);
assert.deepEqual(
  publication.map(({ local, exported }) => [local.name, exported.name]),
  [
    ["ye", "hc"],
    ["gs", "kc"],
    ["Vc", "zj"],
  ]
);
const aliasText = "export { " + publication.map(({ start, end }) => code.slice(start, end)).join(", ") + " };\n";
const ids = [...executable, exportId];
const entry = "entry.mjs";
const helper = "packages/excalidraw/element/typechecks.mjs";
const spec = {
  entry,
  order: [helper],
  sequential: true,
  modules: {
    [helper]: moved.map((i) => "#" + ids.indexOf(i)),
    [entry]: ids.flatMap((i, j) => (moved.includes(i) ? [] : ["#" + j])),
  },
};
const rowText = (i) => {
  const { node, start, end } = units[i];
  assert.equal(node.type, "VariableDeclaration");
  assert.equal(node.kind, "var");
  assert.equal(node.declarations.length, 1);
  assert.equal(node.declarations[0].init.type, "ArrowFunctionExpression");
  // Restore the shared var keyword for each comma-list declarator. The
  // identifier and initializer bytes themselves are copied from the editor.
  const text = code.slice(start, end);
  return (text.startsWith("var ") ? text : "var " + text).replace(/;?$/, ";");
};
const statements = (group) => group.map(rowText).join("\n") + "\n";
const flatCode = statements(executable) + aliasText;
assert.equal(sha256(flatCode), "3653cc7b60e31948d0945861863790696164a9b12348e7fb2d744900cfa4877e");
const flat = path.join(artifacts, "visible-elements.mjs");
writeFileSync(flat, flatCode);
const projected = parse(flatCode);
assert.equal(topLevelStatements(projected.ast).length, ids.length);
assert.deepEqual(projected.scopes.globalScope.through, []);
walk(projected.ast, (node) => {
  assert(!["ImportDeclaration", "ImportExpression", "MetaProperty"].includes(node.type));
});

// Append only the observer to a separate copy. Lm is the original, private
// getVisibleElements; the certificate input keeps exactly the original aliases.
const observer = `
const visibleElementsInputs = [
  { id:"deleted", type:"rectangle", width:4, height:3, isDeleted:true },
  { id:"zero", type:"rectangle", width:0, height:0 },
  { id:"box", type:"rectangle", width:4, height:3 },
  { id:"short-line", type:"line", width:5, height:0, points:[[0,0]] },
  { id:"line", type:"line", width:5, height:0, points:[[0,0],[5,0]] },
  { id:"short-freedraw", type:"freedraw", width:5, height:0, points:[[0,0]] },
  { id:"freedraw", type:"freedraw", width:5, height:0, points:[[0,0],[5,0]] }
];
const visibleElementsActual = Lm(visibleElementsInputs);
console.log(JSON.stringify({
  ids:visibleElementsActual.map(e=>e.id),
  sameObjects:visibleElementsActual.every(e=>visibleElementsInputs.includes(e)),
  linear:ye(visibleElementsInputs[3]),
  smallFreedraw:Vc(visibleElementsInputs[5])
}));
`;
function observe(file) {
  const observed = path.join(path.dirname(file), "observe.mjs");
  writeFileSync(observed, readFileSync(file, "utf8") + "\n" + observer);
  return JSON.parse(execFileSync(process.execPath, [observed], { encoding: "utf8" }));
}
function layout(name, importSt) {
  const dir = path.join(artifacts, name);
  const owner = path.join(dir, helper);
  mkdirSync(path.dirname(owner), { recursive: true });
  writeFileSync(owner, statements(moved) + "export { st, ye, gs };\n");
  const main = path.join(dir, entry);
  writeFileSync(
    main,
    "import { " + importSt + ', ye, gs } from "./' + helper + '";\n' + statements([1699, consumer]) + aliasText
  );
  return main;
}
const expected = { ids: ["box", "line", "freedraw"], sameObjects: true, linear: true, smallFreedraw: true };
assert.deepEqual(observe(flat), expected);
const handWired = observe(layout("reference-layout", "st"));
assert.deepEqual(handWired, expected);
// Rewire only the private import to a different original function. gs
// expects a type string; the original st expects an element object.
const wrong = observe(layout("wrong-cell", "gs as st"));
assert.deepEqual(wrong, { ...expected, ids: ["box", "line", "short-freedraw", "freedraw"], smallFreedraw: false });

const request = {
  source: flat,
  spec: specFile("visible-elements", spec),
  out: path.join(tmp, "region-out"),
  require: "all-outcomes",
};
const region = split(request);
// The plain plan checks placement and ordering before any proof route.
const diagnosticOut = path.join(tmp, "diagnostic-out");
const planPath = path.join(artifacts, "visible-elements-plan.json");
const diagnostic = execFileSync(
  process.env.LOWERSPLIT,
  [
    "--diagnostic",
    writeDeployment(flat, request.spec, diagnosticOut),
    diagnosticOut,
    "--plan",
    planPath,
    "--all-conflicts",
  ],
  { encoding: "utf8" }
);
assert.match(diagnostic, /Diagnostic run/);
assert(!existsSync(diagnosticOut));
const plan = JSON.parse(readFileSync(planPath, "utf8"));
assert.deepEqual(plan.order, [helper, entry]);
assert.deepEqual(plan.modules, { [helper]: [0, 1, 2, 3], [entry]: [4, 5, 6] });
assert.deepEqual(plan.orderConflicts, { count: 0, examples: [], pairs: [] });
const [decoded, indexed] = probe(flat, spec, ["singletonSourceShape", "indexedInlinePlan"]);
assert(decoded.output, JSON.stringify(decoded));
assert.deepEqual(
  decoded.output.modules.map(({ body }) => body.map(([id]) => ids[id])),
  [executable]
);
assert.deepEqual(decoded.output.modules[0].exports, [
  ["hc", "ye"],
  ["kc", "gs"],
  ["zj", "Vc"],
]);
if (region.status === "certified") {
  assert.equal(region.guarantee, "all-outcomes", region.log);
  assert.equal(region.verdict.strategy, "compiled-constructor", region.log);
  assert.deepEqual(split({ ...request, checkOutput: true }).verdict, region.verdict);
  const copied = path.join(artifacts, "emitted");
  cpSync(request.out, copied, { recursive: true });
  assert.deepEqual(observe(path.join(copied, entry)), expected);
} else {
  assert.equal(region.reasonCode, "no_constructed_rule", region.log);
  assert.deepEqual(region.files, []);
}
const report = {
  originalHash,
  full: { pair, verdict: full.verdict, first: fullSource.error, scrollClosureCount: wholeScroll.length },
  region: {
    ids,
    projectionHash: sha256(flatCode),
    spec,
    verdict: region.verdict,
    files: region.files,
    plan,
    decoded: decoded.output,
    indexed: indexed.error ?? indexed.output,
    node: { expected, handWired, wrong },
  },
};
console.log(JSON.stringify(report));
writeFileSync(path.join(artifacts, "result.json"), JSON.stringify(report, null, 2));
