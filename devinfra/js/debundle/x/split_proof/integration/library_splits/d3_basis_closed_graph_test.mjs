import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { split } from "../../translation/testing/split.mjs";

const root = process.env.TEST_TMPDIR;
const entry = "curve/basisClosed.js";
const basis = "curve/basis.js";
const noop = "noop.js";
const helper = "curve/closed-prototype.mjs";
const programPaths = [entry, basis, noop];
const hashes = [
  "d577db3caddaaca6b026cdf801feaebbc78f43227d5ef73130528bfe5261891e",
  "d73b0a859813332b6ee084a97b6bbe41a5d8a421c26675dd84169215cfc2bdfe",
  "bb88c2b91d4fbaa228252328f7ecad919a5a27117e81e7fe4755374570c58563",
];
const original = path.join(root, "original");
mkdirSync(path.join(original, "curve"), { recursive: true });
for (const [i, arg] of process.argv.slice(2).entries()) {
  assert.equal(createHash("sha256").update(readFileSync(arg)).digest("hex"), hashes[i]);
  copyFileSync(arg, path.join(original, programPaths[i]));
}

function writeJson(file, value) {
  writeFileSync(file, JSON.stringify(value));
}
function moduleIo(...args) {
  execFileSync(process.env.MODULE_IO, args, { encoding: "utf8" });
}
function readModules(dir, routes) {
  const input = path.join(root, "routes.json");
  const result = path.join(root, "parsed.json");
  writeJson(input, routes);
  moduleIo("--read-modules", dir, input, result);
  return JSON.parse(readFileSync(result, "utf8"));
}
const source = readModules(original, [entry])[entry];
assert.deepEqual(
  source.body.map((row) => row.type),
  ["ImportDeclaration", "ImportDeclaration", "FunctionDeclaration", "ExpressionStatement", "ExportDefaultDeclaration"]
);
function makeSpec(move) {
  const selector = (i) => `#${i}`;
  return {
    entry,
    modules: {
      [entry]: [0, 1, 2, 3, 4].filter((i) => !move.includes(i)).map(selector),
      [helper]: move.map(selector),
    },
    order: [helper],
    sequential: true,
  };
}
function model(spec, rest) {
  return JSON.parse(
    execFileSync(process.env.BASIS_CLOSED_CHECKER, {
      input: JSON.stringify({ source, spec, ...rest }) + "\n",
      encoding: "utf8",
    })
  );
}
const placement = path.join(root, "spec.json");
const manifest = path.join(root, "deployment.json");
writeJson(manifest, {
  entry,
  chunks: programPaths.map((route) => ({
    path: route,
    source: path.join(original, route),
    ...(route === entry ? { spec: placement } : {}),
  })),
});

function stage(dir, spec, adjust = () => {}) {
  const planned = model(spec);
  adjust(planned.programs);
  const programsFile = path.join(root, "programs.json");
  writeJson(programsFile, planned.programs);
  mkdirSync(dir);
  moduleIo("--print", programsFile, dir, path.join(original, entry));
  for (const name of [basis, noop]) {
    copyFileSync(path.join(original, name), path.join(dir, name));
    assert.deepEqual(readFileSync(path.join(dir, name)), readFileSync(path.join(original, name)));
  }
  return planned;
}
function reread(dir, spec) {
  return model(spec, { files: readModules(dir, [entry, helper]), fixed: readModules(dir, [basis, noop]) });
}

function observe(dir) {
  const url = (name) => JSON.stringify(pathToFileURL(path.join(dir, name)).href);
  const script = `
import assert from "node:assert/strict";
import make, * as api from ${url(entry)};
import noop from ${url(noop)};
import { point } from ${url(basis)};
let trace = [];
const sentinel = new Error("path sentinel");
const context = {
  moveTo(...xy) { assert.equal(this, context); trace.push(["move", ...xy]); },
  bezierCurveTo(...xy) { assert.equal(this, context); trace.push(["curve", ...xy]); },
};
const closed = make(context);
const second = make(context);
assert.notEqual(closed, second);
assert.equal(Object.getPrototypeOf(closed), Object.getPrototypeOf(second));
for (const method of ["areaStart", "areaEnd"]) assert.equal(closed[method], noop);
closed.lineStart();
for (const x of [0, 6, 12, 18]) closed.point(x, x);
const drawn = trace;
trace = [];
point({_context: context, _x0: 6, _x1: 12, _y0: 6, _y1: 12}, 18, 18);
assert.deepEqual(trace[0], drawn[1]);
context.bezierCurveTo = function () { assert.equal(this, context); throw sentinel; };
second.lineStart();
for (const x of [0, 6, 12]) second.point(x, x);
assert.throws(() => second.point(18, 18), (thrown) => thrown === sentinel);
console.log(JSON.stringify({ publicNames: Object.keys(api), drawn, partial: [second._point, second._x0, second._x1] }));
`;
  return JSON.parse(
    execFileSync(process.execPath, ["--experimental-default-type=module", "--input-type=module", "-e", script], {
      encoding: "utf8",
    })
  );
}

const baseline = observe(original);
assert.deepEqual(baseline, {
  publicNames: ["default"],
  drawn: [
    ["move", 6, 6],
    ["curve", 8, 8, 10, 10, 12, 12],
  ],
  partial: [3, 6, 12],
});
const alone = makeSpec([3]);
const prototypeOnly = path.join(root, "prototype-only");
const single = stage(prototypeOnly, alone);
assert.deepEqual(single.plan.entryGlueExports, [["BasisClosed", "BasisClosed"]]);
const leak = reread(prototypeOnly, alone);
assert.match(leak.refusal, /entry exports resolve to different linked cells; extra 1: "BasisClosed"/);
assert.deepEqual(observe(prototypeOnly), { ...baseline, publicNames: ["BasisClosed", "default"] });

const together = makeSpec([2, 3]);
const compatible = path.join(root, "compatible");
stage(compatible, together);
const checked = reread(compatible, together);
assert.equal(checked.plan.valid, true);
assert.deepEqual(
  checked.plan.plannedRows.toSorted((a, b) => a[0] - b[0]),
  [
    [0, entry],
    [1, entry],
    [2, helper],
    [3, helper],
    [4, entry],
  ]
);
assert.equal(checked.publicCells, "same", JSON.stringify(checked));
assert.equal(checked.completeOutcomes, "same");
assert.deepEqual(checked.sourceOrder, [noop, basis, entry]);
assert.deepEqual(checked.outputOrder, [noop, basis, helper, entry]);
assert.deepEqual(checked.sourceUnits, checked.outputUnits);
assert.deepEqual(
  checked.executableOwners.filter(([id]) => id < 5).toSorted((a, b) => a[0] - b[0]),
  [
    [2, helper],
    [3, helper],
    [4, entry],
  ]
);
assert.deepEqual(observe(compatible), baseline);
// Substituting a different export from the same provider keeps the route and
// postorder; the generated method would still call the wrong cell.
const wrong = path.join(root, "wrong-cell");
stage(wrong, together, (programs) => {
  const request = programs[helper].body.find(
    (row) => row.type === "ImportDeclaration" && row.source.value === "./basis.js"
  );
  assert.equal(request.specifiers[0].imported.name, "point");
  request.specifiers[0].imported.name = "Basis";
});
const changed = reread(wrong, together);
assert.match(changed.refusal, /unexpected provider import from curve\/basis\.js/);
console.log(JSON.stringify({ prototypeOnly: leak, coMoved: checked, node: baseline }));

for (const spec of [alone, together]) {
  writeJson(placement, spec);
  const verdict = split({ manifest, out: path.join(root, "uncertified"), require: "all-outcomes" });
  assert.equal(verdict.status, "refused", verdict.log);
  assert.match(verdict.reason, /no constructor supports multi-chunk deployments/);
}
