import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

const [sourceArg, callerArg] = process.argv.slice(2);
const [source, caller] = [sourceArg, callerArg].map((file) => path.resolve(file));
const code = readFileSync(source, "utf8");
function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}
assert.equal(sha256(code), "d73b0a859813332b6ee084a97b6bbe41a5d8a421c26675dd84169215cfc2bdfe");
assert.equal(sha256(readFileSync(caller)), "8ea2ba769352b26a109f89f0c67770cb552fe6346162ef1f783af70d48fe5e25");
const parse = (text) => parseModule(text, acorn.parse, eslintScope);
const parsed = parse(code);
const free = [...new Set(parsed.scopes.globalScope.through.map(({ identifier }) => identifier.name))];
assert.deepEqual(free, ["NaN"]); // A standard, immutable primitive binding already modeled by CoreJS.
const rows = topLevelStatements(parsed.ast).map(({ node }) => node);
assert.deepEqual(
  rows.map(({ type }) => type),
  ["ExportNamedDeclaration", "ExportNamedDeclaration", "ExpressionStatement", "ExportDefaultDeclaration"]
);
assert.deepEqual(
  rows.slice(0, 2).map(({ declaration }) => declaration.id.name),
  ["point", "Basis"]
);
assert.equal(rows[3].declaration.id, null);
assert.equal(rows[2].expression.left.property.name, "prototype");
const actualImport = parse(readFileSync(caller, "utf8")).ast.body[0];
assert.equal(actualImport.source.value, "./basis.js");
assert.equal(actualImport.specifiers[0].imported.name, "Basis");

// d3-shape publishes src/ as ESM with a legacy package manifest. Run its
// shipped caller and target on disk as modules without editing either file.
function observe(basis, bundle) {
  const script = `
import assert from "node:assert/strict";
import factory, * as api from ${JSON.stringify(pathToFileURL(basis).href)};
import bundle from ${JSON.stringify(pathToFileURL(bundle).href)};
assert.deepEqual(Object.keys(api), ["Basis", "default", "point"]);
const {Basis, point} = api;
const pathCalls = [];
const context = {
  moveTo(x,y) { assert.equal(this, context); pathCalls.push(["move",x,y]); },
  lineTo(x,y) { assert.equal(this, context); pathCalls.push(["line",x,y]); },
  bezierCurveTo(...coords) { assert.equal(this, context); pathCalls.push(["curve",...coords]); },
  closePath() { assert.equal(this, context); pathCalls.push(["close"]); },
};
const instance = bundle.beta(1)(context);
assert(instance instanceof Basis);
assert.equal(instance._context, context);
assert.notEqual(factory(context), instance);
assert.equal(factory(context).point, instance.point);
point({_context: context, _x0: 0, _y0: 0, _x1: 6, _y1: 6}, 12, 12);
const directPoint = pathCalls.pop();
assert.deepEqual(directPoint, ["curve",2,2,4,4,6,6]);
instance.lineStart();
assert.equal(instance._point, 0);
for (const x of [0, 6, 12]) instance.point(x,x);
instance.lineEnd();
assert.deepEqual(pathCalls, [
  ["move",0,0], ["line",1,1], ["curve",2,2,4,4,6,6], ["curve",8,8,10,10,11,11], ["line",12,12]
]);
assert.deepEqual([instance._point, instance._x0, instance._y0, instance._x1, instance._y1], [3, 6, 6, 12, 12]);
const pathBeforeThrow = pathCalls.length;
const failure = new Error("supplied bezier failure");
context.bezierCurveTo = function (...coords) {
  assert.equal(this,context);
  pathCalls.push(["curve", ...coords]);
  throw failure;
};
const failed = bundle.beta(1)(context);
failed.lineStart();
failed.point(0,0);
failed.point(6,6);
assert.throws(() => failed.point(12,12), (value) => value === failure);
assert.deepEqual([failed._point, failed._x0, failed._x1], [3, 0, 6]);
assert.deepEqual([failed._y0, failed._y1], [0, 6]);
assert.deepEqual(pathCalls.slice(pathBeforeThrow), [
  ["move",0,0], ["line",1,1], ["curve",2,2,4,4,6,6]
]);
console.log(JSON.stringify({
  directPoint, pathCalls:pathCalls.slice(0,pathBeforeThrow), beforeThrow:pathCalls.slice(pathBeforeThrow),
  earlyThrow:true, partial:[failed._point, failed._x0, failed._x1]
}));
`;
  return JSON.parse(
    execFileSync(process.execPath, ["--experimental-default-type=module", "--input-type=module", "-e", script], {
      encoding: "utf8",
    })
  );
}

const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const entry = "basis.js";
const helper = "spline.mjs";
const spec = { entry, modules: { [helper]: ["#0"], [entry]: ["#1", "#2", "#3"] }, order: [helper], sequential: true };
const specFile = path.join(artifacts, "basis-placement.json");
writeFileSync(specFile, JSON.stringify(spec, null, 2));
const node = observe(source, caller);
const original = dumpParsed(parsed, { includeOpaqueCaptures: true });
const [decoded, inline] = JSON.parse(
  execFileSync(process.env.LINKED_INPUT_CHECKER, {
    input:
      JSON.stringify([
        { kind: "sourceClassification", input: { source: original, entry } },
        { kind: "indexedInlinePlan", input: { source: original, sourceText: code, spec } },
      ]) + "\n",
    encoding: "utf8",
  })
);
const request = {
  source,
  spec: specFile,
  out: path.join(process.env.TEST_TMPDIR, "basis-split"),
  require: "all-outcomes",
};
const result = split(request);
console.log(JSON.stringify({ spec, node, decoded, inline, verdict: result.verdict, files: result.files }));
assert.deepEqual(decoded.output.units, [0, 1, 2, 3]);
assert.deepEqual(decoded.output.imports, []);
assert.equal(inline.error, "expected simple top-level declaration rows");
assert.equal(result.status, "refused", result.log);
assert.equal(result.reasonCode, "no_constructed_rule", result.log);

function checkPlan(spec) {
  return JSON.parse(
    execFileSync(process.env.PLACEMENT_CHECKER, {
      input: JSON.stringify({ source: original, sourceText: code, spec }) + "\n",
      encoding: "utf8",
    })
  );
}
const movedExport = checkPlan(spec);
const privateSpec = { ...spec, modules: { [helper]: ["#2"], [entry]: ["#0", "#1", "#3"] } };
const movedPrototype = checkPlan(privateSpec);
writeFileSync(path.join(artifacts, "basis-prototype-placement.json"), JSON.stringify(privateSpec, null, 2));
const prototypeRequest = {
  ...request,
  spec: path.join(artifacts, "basis-prototype-placement.json"),
  out: path.join(process.env.TEST_TMPDIR, "basis-prototype"),
};
const privateResult = split(prototypeRequest);
console.log(JSON.stringify({ movedExport, movedPrototype, privateVerdict: privateResult.verdict }));

assert.equal(privateResult.status, "certified", privateResult.log);
assert.equal(privateResult.scope, "indexed-prototype-write", privateResult.log);
assert.equal(privateResult.guarantee, "all-outcomes", privateResult.log);
assert.deepEqual(privateResult.files, [entry, helper]);
const recheck = split({ ...prototypeRequest, checkOutput: true });
assert.deepEqual(recheck.verdict, privateResult.verdict);
const emittedEntry = path.join(prototypeRequest.out, entry);
const emittedHelper = path.join(prototypeRequest.out, helper);
const emittedCode = readFileSync(emittedHelper, "utf8");
const helperRows = parse(emittedCode).ast.body;
assert.deepEqual(
  helperRows.map(({ type }) => type),
  ["ImportDeclaration", "ExpressionStatement"]
);
assert.equal(helperRows[0].source.value, "./basis.js");
assert.deepEqual(helperRows[0].specifiers.map(({ imported }) => imported.name).sort(), ["Basis", "point"]);
assert.equal(emittedCode.slice(helperRows[1].start, helperRows[1].end), code.slice(rows[2].start, rows[2].end));

// Change a literal in the emitted method without changing either file's
// paths or public names. The disk check must reject the changed behavior.
const lineStart = helperRows[1].expression.right.properties.find(({ key }) => key.name === "lineStart");
const pointInit = lineStart.value.body.body.at(-1).expression;
assert.equal(pointInit.left.property.name, "_point");
assert.equal(pointInit.right.type, "Literal");
assert.equal(pointInit.right.value, 0);
const { start, end } = pointInit.right;
writeFileSync(emittedHelper, emittedCode.slice(0, start) + "1" + emittedCode.slice(end));
let tamperedVerdict;
try {
  const observed = execFileSync(
    process.execPath,
    [
      "--experimental-default-type=module",
      "--input-type=module",
      "-e",
      `const api = await import(process.argv[1]);
       const curve = api.default({});
       curve.lineStart();
       console.log(JSON.stringify({ names: Object.keys(api), point: curve._point }));`,
      pathToFileURL(emittedEntry).href,
    ],
    { encoding: "utf8" }
  );
  assert.deepEqual(JSON.parse(observed), { names: ["Basis", "default", "point"], point: 1 });
  // split() snapshots the existing tree and asserts that a refusal did not change it.
  const tampered = split({ ...prototypeRequest, checkOutput: true });
  assert.equal(tampered.status, "refused", tampered.log);
  assert.match(tampered.reason, /output module spline\.mjs differs from its generated ESTree/);
  tamperedVerdict = tampered.verdict;
} finally {
  writeFileSync(emittedHelper, emittedCode);
}

const shippedCaller = path.join(prototypeRequest.out, "bundle.js");
copyFileSync(caller, shippedCaller);
assert.equal(sha256(readFileSync(shippedCaller)), sha256(readFileSync(caller)));
const emitted = observe(emittedEntry, shippedCaller);
assert.deepEqual(emitted, node);
copyFileSync(emittedEntry, path.join(artifacts, "emitted-basis.js"));
copyFileSync(emittedHelper, path.join(artifacts, "emitted-spline.mjs"));
for (const plan of [movedExport, movedPrototype]) {
  assert.equal(plan.valid, true);
  assert.deepEqual(plan.sourcePublic, ["point", "Basis", "default"]);
  const expectedDraw = [
    { trace: 12332, threw: false, sameContext: true, fields: [3, 6, 12] },
    { trace: 123, threw: true, sameContext: true, fields: node.partial },
  ];
  assert.deepEqual(plan.draw, [expectedDraw, expectedDraw]);
}
assert.deepEqual(movedExport.order, [0, 1, 2, 3]);
assert.deepEqual(movedExport.candidatePublic, ["Basis", "default"]);
assert.equal(movedExport.sameExports, false);
assert.deepEqual(movedPrototype.order, [2, 0, 1, 3]);
assert.deepEqual(movedPrototype.candidatePublic, ["point", "Basis", "default"]);
assert.equal(movedPrototype.sameExports, true);
writeFileSync(
  path.join(artifacts, "basis-results.json"),
  JSON.stringify(
    {
      original: { node, decoded, inline },
      emitted,
      movedExport,
      movedPrototype,
      verdicts: [result.verdict, privateResult.verdict, recheck.verdict, tamperedVerdict],
    },
    null,
    2
  )
);
