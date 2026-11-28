import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { runInNewContext } from "node:vm";
import { dumpParsed } from "../../translation/annotated_estree.mjs";
import { acorn, eslintScope } from "../../translation/parser.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";

const chunk = "chunk-X4ON7R5K.js";
const bundle = process.env.EXCALIDRAW_OUTPUTS.split(" ").find((file) => file.endsWith("/bundle"));
assert(bundle, "missing Excalidraw production bundle");
const source = path.join(bundle, chunk);
const code = readFileSync(source, "utf8");
assert.equal(
  createHash("sha256").update(code).digest("hex"),
  "385d6b35fb88d57a77c6e3b6e85fbf21cd308fc75ce139da2cf0103f414cba92"
);
const parse = (text) => parseModule(text, acorn.parse, eslintScope);
const units = topLevelStatements(parse(code).ast);
assert.equal(units.length, 73);
for (const [id, name, parent] of [
  [6, "T", undefined],
  [8, "H", "T"],
  [9, "N", "T"],
  [10, "V", undefined],
]) {
  const { id: binding, init } = units[id].node.declarations[0];
  assert.equal(binding.name, name);
  assert.equal(init.type, "ClassExpression");
  assert.equal(init.superClass?.name, parent);
}
const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
function placement(count, moved, entry = "entry.mjs") {
  return {
    entry,
    sequential: true,
    order: ["prefix.mjs"],
    modules: {
      "prefix.mjs": moved.map((i) => "#" + i),
      [entry]: Array.from({ length: count }, (_, i) => i)
        .filter((i) => !moved.includes(i))
        .map((i) => "#" + i),
    },
  };
}

// Full, unchanged input. The first nine executed source rows end in unit 10
// rather than unit 8; only pairs (10, 8) and (10, 9) are inverted.
const actualSpec = placement(units.length, [0, 1, 2, 3, 4, 5, 6, 7, 10], chunk);

const slice = (i) => code.slice(units[i].start, units[i].end);
const parent = slice(6);
const h = slice(8);
const n = slice(9);
const constructorProbe = [
  "const helper = {};",
  "const h = new H(helper);",
  "const n = new N(helper);",
  "if (h.helper !== helper || n.helper !== helper) throw 0;",
  "if (h.renderLines !== H.prototype.renderLines || n._fillPolygons !== N.prototype._fillPolygons) throw 1;",
].join("\n");
const projected = [parent, h, n, constructorProbe].join("\n");
const replaced = [parent, "T = 0;", h, n, constructorProbe].join("\n");

// Node gets hostile ambient properties: neither class creation nor forwarding
// to T's real constructor may touch Object.assign or Math in deferred methods.
const node = (text) =>
  runInNewContext('"use strict";\n' + text, {
    get Object() {
      throw Error("eager Object access");
    },
    get Math() {
      throw Error("eager Math access");
    },
  });
node(projected);
node([parent, slice(10), h, n, constructorProbe].join("\n"));
assert.throws(() => node(replaced), { name: "TypeError" });

// This swap would change a throw during class creation into success: move
// both subclass declarations before T=0, after the original parent allocation.
node([parent, h, n, "T = 0;", constructorProbe].join("\n"));
const replacedClasses = [parent, "T = 0;", h, n].join("\n");
const ast = (text) => dumpParsed(parse(text), { includeOpaqueCaptures: true });
const requests = [
  ...[projected, replaced].map((text) => ({
    kind: "moduleDiagnostic",
    input: { path: "entry.mjs", ast: ast(text) },
  })),
  { kind: "heritagePlan", input: { source: ast(code), spec: actualSpec } },
  { kind: "heritagePlan", input: { source: ast(replacedClasses), spec: placement(4, [0, 2, 3]) } },
];
const model = spawnSync(process.env.LINKED_INPUT_CHECKER, [], {
  input: JSON.stringify(requests) + "\n",
  encoding: "utf8",
  timeout: 90_000,
});
assert.ifError(model.error);
assert.equal(model.status, 0, model.stderr);
writeFileSync(path.join(artifacts, "heritage.json"), model.stdout);
const [ok, bad, actual, negative] = JSON.parse(model.stdout);
assert.deepEqual(ok.output?.module?.output?.isolatedExecution, { status: "ok" }, model.stdout);
assert.deepEqual(bad.output?.module?.output?.isolatedExecution, { reason: "throw", unit: 2 }, model.stdout);
assert.deepEqual(
  actual.output,
  {
    inversions: [
      [10, 8],
      [10, 9],
    ],
    valid: true,
    footprints: true,
    ordinaryMoves: false,
    heritageMoves: true,
  },
  JSON.stringify(actual)
);
assert.deepEqual(
  negative.output?.inversions,
  [
    [2, 1],
    [3, 1],
  ],
  JSON.stringify(negative)
);
assert.equal(negative.output?.ordinaryMoves, false);
assert.equal(negative.output?.heritageMoves, false);
