import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { acorn, eslintScope } from "../../translation/parser.mjs";
import { parseModule, topLevelStatements } from "../../translation/scope_metadata.mjs";
import { split } from "../../translation/testing/split.mjs";

const entry = "chunk-X4ON7R5K.js";
const bundle = process.env.EXCALIDRAW_OUTPUTS.split(" ").find((file) => file.endsWith("/bundle"));
assert(bundle);
const source = path.join(bundle, entry);
const bytes = readFileSync(source);
assert.equal(
  createHash("sha256").update(bytes).digest("hex"),
  "385d6b35fb88d57a77c6e3b6e85fbf21cd308fc75ce139da2cf0103f414cba92"
);
const units = topLevelStatements(parseModule(bytes.toString(), acorn.parse, eslintScope).ast);
assert.equal(units.length, 73);
const { node } = units[72];
assert.equal(node.type, "ExportNamedDeclaration");
assert.equal(node.source, null);
assert.deepEqual(
  node.specifiers.map(({ local, exported }) => [local.name, exported.name]),
  [
    ["Z", "a"],
    ["L", "b"],
    ["is", "c"],
  ]
);

const root = process.env.TEST_TMPDIR;
const artifacts = process.env.TEST_UNDECLARED_OUTPUTS_DIR;
const provider = "prefix.mjs";
function placement(name, selected) {
  const moved = new Set(selected);
  const file = path.join(artifacts, name + ".json");
  writeFileSync(
    file,
    JSON.stringify({
      entry,
      sequential: true,
      order: [provider],
      modules: {
        [provider]: selected.map((ordinal) => "#" + ordinal),
        [entry]: units.flatMap((_, ordinal) => (moved.has(ordinal) ? [] : ["#" + ordinal])),
      },
    })
  );
  return file;
}
const spec = placement(
  "placement",
  Array.from({ length: 11 }, (_, i) => i)
);
const out = path.join(root, "split");
const request = { source, spec, out, require: "normal-completion", timeoutMs: 90_000 };
const emitted = split(request);
assert.equal(emitted.status, "certified", emitted.log);
assert.equal(emitted.scope, "indexed-class-prefix", emitted.log);
assert.equal(emitted.guarantee, "normal-completion");
assert.equal(emitted.verdict.every_ending_matches, false);
assert.deepEqual(emitted.files, [entry, provider]);
const disk = split({ ...request, checkOutput: true });
assert.equal(disk.status, "certified", disk.log);
assert.equal(disk.scope, emitted.scope);
assert.equal(disk.guarantee, emitted.guarantee);

const strict = split({ ...request, require: "all-outcomes", out: path.join(root, "strict") });
assert.equal(strict.status, "refused", strict.log);
const reordered = split({
  ...request,
  spec: placement("reordered", [0, 1, 2, 3, 4, 5, 6, 7, 10]),
  out: path.join(root, "reordered"),
});
assert.equal(reordered.status, "refused", reordered.log);

const wrapper = "vendor/roughjs.js";
async function loadGraph(output, viaWrapper) {
  const context = vm.createContext({});
  const effects = [];
  // Use a stateful supplied random function to observe the order of actual public calls.
  context.recordRandom = () => {
    effects.push(["random"]);
    return 0.125;
  };
  vm.runInContext("Math.random = recordRandom", context);
  const modules = new Map();
  function module(route) {
    if (modules.has(route)) return modules.get(route);
    assert(!route.startsWith("../") && !path.isAbsolute(route));
    const file = path.join(output && emitted.files.includes(route) ? output : bundle, route);
    const m = new vm.SourceTextModule(readFileSync(file, "utf8"), { context, identifier: route });
    modules.set(route, m);
    return m;
  }
  const rootModule = module(viaWrapper ? wrapper : entry);
  await rootModule.link((request, parent) => {
    assert(request.startsWith("./") || request.startsWith("../"), request);
    return module(path.posix.normalize(path.posix.join(path.posix.dirname(parent.identifier), request)));
  });
  await rootModule.evaluate();
  const publicModule = modules.get(entry).namespace;
  assert.deepEqual(Object.keys(publicModule), ["a", "b", "c"]);
  if (viaWrapper) {
    assert.deepEqual(Object.keys(rootModule.namespace), ["default"]);
    assert.equal(rootModule.namespace.default, publicModule.c);
  }
  assert.deepEqual(effects, [], "evaluation must not draw or consume random values");
  return { publicModule, effects, routes: [...modules.keys()].sort() };
}

async function observe(output, viaWrapper) {
  const { publicModule: api, effects, routes } = await loadGraph(output, viaWrapper);
  const random = new api.a(123);
  assert.equal(random.constructor, api.a);
  const gen = api.c.generator({ options: { seed: 123 } });
  assert.equal(gen.constructor, api.b);
  assert.equal(Object.getPrototypeOf(gen), api.b.prototype);
  const drawOptions = { seed: 123, fill: "#090", fillStyle: "zigzag" };
  const rectangle = gen.rectangle(1, 2, 13, 17, drawOptions);
  assert(rectangle.sets.some(({ type }) => type === "fillSketch"));
  const methods = [
    "save",
    "restore",
    "beginPath",
    "moveTo",
    "lineTo",
    "bezierCurveTo",
    "stroke",
    "fill",
    "setLineDash",
  ];
  const canvas = {
    getContext(kind) {
      effects.push(["getContext", kind]);
      return Object.fromEntries(methods.map((method) => [method, (...args) => effects.push([method, ...args])]));
    },
  };
  const drawing = api.c.canvas(canvas);
  assert.equal(drawing.generator.constructor, api.b);
  drawing.line(1, 2, 3, 4, { seed: 123 });
  const next = random.next();
  const seed = api.c.newSeed();
  assert(effects.some(([kind]) => kind === "stroke"));
  assert.equal(effects.at(-1)[0], "random");
  return {
    routes,
    values: JSON.parse(JSON.stringify({ rectangle, next, seed })),
    effects,
    publicFunctions: [api.a, api.b, api.c.generator, api.c.canvas, api.c.newSeed].map((fn) => [
      fn.name,
      Function.prototype.toString.call(fn),
    ]),
  };
}

const observations = [];
for (const viaWrapper of [false, true]) {
  const original = await observe(null, viaWrapper);
  const transformed = await observe(out, viaWrapper);
  assert.deepEqual(transformed.routes, [...original.routes, provider].sort());
  assert.deepEqual(transformed.values, original.values);
  assert.deepEqual(transformed.effects, original.effects);
  assert.deepEqual(transformed.publicFunctions, original.publicFunctions);
  observations.push({
    viaWrapper,
    originalRoutes: original.routes,
    outputRoutes: transformed.routes,
    effects: original.effects,
  });
}

// Keep the published name a but point it at b's cell. The independently
// parsed artifact check must reject a file with the same namespace keys.
const entryFile = path.join(out, entry);
const published = readFileSync(entryFile, "utf8");
const outputProgram = parseModule(published, acorn.parse, eslintScope).ast;
const wrong = outputProgram.body.flatMap((node) => node.specifiers ?? []).find((sp) => sp.exported?.name === "a");
assert.equal(wrong.local.name, "Z");
const damaged = published.slice(0, wrong.local.start) + "L" + published.slice(wrong.local.end);
writeFileSync(entryFile, damaged);
assert.equal(split({ ...request, checkOutput: true }).status, "refused");
assert.equal(readFileSync(entryFile, "utf8"), damaged);
writeFileSync(entryFile, published);
writeFileSync(
  path.join(artifacts, "outcomes.json"),
  JSON.stringify(
    {
      originalUnits: units.length,
      movedUnits: 11,
      retainedExecutableUnits: 61,
      terminalRows: 1,
      projectionUsed: false,
      inputBytes: bytes.length,
      outputBytes: Object.fromEntries(emitted.files.map((name) => [name, readFileSync(path.join(out, name)).length])),
      emitted: emitted.verdict,
      disk: disk.verdict,
      strict: strict.verdict,
      reordered: reordered.verdict,
      observations,
    },
    null,
    2
  )
);
