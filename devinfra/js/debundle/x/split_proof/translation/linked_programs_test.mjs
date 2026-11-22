import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
import { generate } from "astring";

const root = process.env.TEST_TMPDIR;
assert(root, "TEST_TMPDIR is required");

const id = (name) => ({ type: "Identifier", name });
const literal = (value) => ({ type: "Literal", value });
const call = (callee, args = []) => ({ type: "CallExpression", callee, arguments: args });
const expression = (expression) => ({ type: "ExpressionStatement", expression });
const returns = (argument) => ({ type: "ReturnStatement", argument });
const block = (body) => ({ type: "BlockStatement", body });
const arrow = (params, body) => ({
  type: "ArrowFunctionExpression",
  params,
  body,
  expression: body.type !== "BlockStatement",
});
const functionExpression = (params, body) => ({ type: "FunctionExpression", id: null, params, body });
const variable = (kind, name, init) => ({
  type: "VariableDeclaration",
  kind,
  declarations: [{ type: "VariableDeclarator", id: id(name), init }],
});
const importNamed = (source, bindings) => ({
  type: "ImportDeclaration",
  source: literal(source),
  specifiers: bindings.map(([imported, local]) => ({
    type: "ImportSpecifier",
    imported: id(imported),
    local: id(local),
  })),
});
const exportNamed = (bindings) => ({
  type: "ExportNamedDeclaration",
  declaration: null,
  specifiers: bindings.map(([local, exported]) => ({
    type: "ExportSpecifier",
    local: id(local),
    exported: id(exported),
  })),
  source: null,
});
const program = (body) => generate({ type: "Program", sourceType: "module", body });

const probe = path.join(root, "linked_programs_observe.mjs");
writeFileSync(
  probe,
  `try {
  const module = await import(process.argv[2]);
  const names = JSON.parse(process.argv[3]);
  const values = Object.fromEntries(names.map((name) => {
    const exported = module[name];
    const value = typeof exported === "function" ? exported() : exported;
    return [name, typeof value === "function" ? value() : value];
  }));
  console.log(JSON.stringify({ values }));
} catch (error) {
  console.log(JSON.stringify({ error: error.name }));
}
`
);

function observe(file, exportNames) {
  const names = Array.isArray(exportNames) ? exportNames : [exportNames];
  const result = spawnSync(process.execPath, [probe, pathToFileURL(file).href, JSON.stringify(names)], {
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.error?.message ?? result.stdout + result.stderr);
  const observed = JSON.parse(result.stdout.trim());
  if (observed.error) return observed;
  return Object.fromEntries(names.map((name) => [name, observed.values[name]]));
}

function lower(source, spec, out, extra = []) {
  const result = spawnSync(process.env.LOWERSPLIT, [source, spec, out, "--require", "normal-completion", ...extra], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.error) throw result.error;
  const log = result.stdout + result.stderr;
  assert([0, 1].includes(result.status), log);
  const verdictLines = result.stdout.split(/\r?\n/).filter((line) => {
    try {
      JSON.parse(line);
      return true;
    } catch {
      return false;
    }
  });
  assert.equal(verdictLines.length, 1, log);
  const verdict = JSON.parse(verdictLines[0]);
  assert.equal(verdict.requested, "normal-completion", log);
  assert.equal(verdict.scope, "linked-programs", log);
  assert.equal(result.status, verdict.status === "certified" ? 0 : 1, log);
  return { verdict, log };
}

function prepare(dir, { source, provider, extras = {}, sourceName = "source.mjs", spec }) {
  mkdirSync(dir, { recursive: true });
  const sourceFile = path.join(dir, sourceName);
  const providerFile = path.join(dir, "provider.mjs");
  const specFile = path.join(dir, "spec.json");
  writeFileSync(sourceFile, source);
  writeFileSync(providerFile, provider);
  for (const [name, contents] of Object.entries(extras)) writeFileSync(path.join(dir, name), contents);
  writeFileSync(specFile, JSON.stringify(spec, null, 2));
  return { sourceFile, providerFile, specFile };
}

function generatedCase(seed) {
  const publicFactory = `create${seed}`;
  const sameValueFactory = `sameValue${seed}`;
  const localFactory = `imported${seed}`;
  const resultName = `publicResult${seed}`;
  const plainResultName = `plainResult${seed}`;
  const loaderName = `loader${seed}`;
  const movedName = `moved${seed}`;
  const ballastName = `ballast${seed}`;
  const plainName = `plain${seed}`;
  const fragment = `fragment-${seed}.mjs`;
  const providerMode = seed % 3;
  const hasSecondMovedDeclaration = seed % 2 === 1;
  const callback = providerMode === 0 ? arrow([], id(movedName)) : arrow([], literal(seed + 70 + providerMode));
  const sourceBody = [
    importNamed("./provider.mjs", [[publicFactory, localFactory]]),
    variable("const", ballastName, literal(seed)),
    variable(seed % 2 === 0 ? "const" : "var", loaderName, call(id(localFactory), [callback])),
    variable("const", movedName, arrow([], literal(seed + 20))),
    ...(hasSecondMovedDeclaration ? [variable("const", `fragmentValue${seed}`, literal(seed + 40))] : []),
    variable("const", plainName, arrow([], literal(seed + 60))),
    exportNamed([
      [loaderName, resultName],
      [plainName, plainResultName],
    ]),
  ];
  const movedIndex = 3;
  const extraIndex = 4;
  const plainIndex = hasSecondMovedDeclaration ? 5 : 4;
  const exportedIndex = sourceBody.length - 1;
  const fragmentIndices = [movedIndex, ...(hasSecondMovedDeclaration ? [extraIndex] : [])];
  const fragmentSet = new Set(fragmentIndices);
  const entryIndices = sourceBody.map((_, index) => index).filter((index) => !fragmentSet.has(index));
  const spec = {
    entry: "entry.mjs",
    order: [fragment],
    sequential: true,
    modules: {
      "entry.mjs": entryIndices.map((index) => `#${index}`),
      [fragment]: fragmentIndices.map((index) => `#${index}`),
    },
  };
  assert(entryIndices.includes(plainIndex));
  assert(entryIndices.includes(exportedIndex));
  const factory =
    providerMode === 0
      ? arrow([id("callback")], arrow([], call(id("callback"))))
      : providerMode === 1
        ? arrow([id("callback")], block([expression(call(id("callback"))), returns(arrow([], call(id("callback"))))]))
        : functionExpression([id("callback")], block([returns(call(id("callback")))]));
  const provider = program([
    variable("var", "factory", factory),
    variable("var", "twin", id("factory")),
    exportNamed([
      ["factory", publicFactory],
      ["twin", sameValueFactory],
    ]),
  ]);
  return {
    source: program(sourceBody),
    provider,
    spec,
    fragment,
    publicFactory,
    sameValueFactory,
    localFactory,
    resultName,
    plainResultName,
    providerMode,
    expectedValue: providerMode === 0 ? seed + 20 : providerMode === 1 ? seed + 71 : seed + 72,
  };
}

function eagerProvider() {
  return program([
    variable(
      "var",
      "factory",
      arrow([id("callback")], block([expression(call(id("callback"))), returns(arrow([], call(id("callback"))))]))
    ),
    exportNamed([["factory", "create"]]),
  ]);
}

// Generated aliases, layouts and provider-body shapes exercise the ordinary
// source-file CLI. The entry also owns a separate callable returning a primitive.
for (let seed = 0; seed < 8; seed++) {
  const fixture = generatedCase(seed);
  const dir = path.join(root, `generated-${seed}`);
  const files = prepare(dir, fixture);
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  const exportNames = [fixture.resultName, fixture.plainResultName];
  const original = observe(files.sourceFile, exportNames);
  assert.deepEqual(
    original,
    { [fixture.resultName]: fixture.expectedValue, [fixture.plainResultName]: seed + 60 },
    `seed ${seed}`
  );
  if (fixture.providerMode === 1 && verdict.status === "rejected") {
    assert(!existsSync(out), "an equivalent eager provider may be conservatively refused");
    continue;
  }
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", JSON.stringify(verdict));
  assert(existsSync(path.join(out, "entry.mjs")), "the entry module is published");
  assert(existsSync(path.join(out, fixture.fragment)), "the moved provider module is published");
  assert(existsSync(path.join(out, "provider.mjs")), "the actual named provider is published beside the split");
  assert.deepEqual(observe(path.join(out, "entry.mjs"), exportNames), original, `seed ${seed}`);

  if (seed === 0) {
    assert.equal(lower(files.sourceFile, files.specFile, out, ["--check-output"]).verdict.status, "certified");
    const providerFile = path.join(out, "provider.mjs");
    const providerSource = readFileSync(providerFile, "utf8");
    const changedProvider = providerSource.replace("callback()", "0");
    assert.notEqual(changedProvider, providerSource, providerSource);
    writeFileSync(providerFile, changedProvider);
    const providerMutation = lower(files.sourceFile, files.specFile, out, ["--check-output"]);
    assert.equal(providerMutation.verdict.status, "rejected", providerMutation.log);
    writeFileSync(providerFile, providerSource);

    const entryFile = path.join(out, "entry.mjs");
    const entrySource = readFileSync(entryFile, "utf8");
    const link = new RegExp(`\\b${fixture.publicFactory}(?=\\s+as\\s+${fixture.localFactory}\\b)`);
    assert.match(entrySource, link);
    writeFileSync(entryFile, entrySource.replace(link, fixture.sameValueFactory));
    const mutated = lower(files.sourceFile, files.specFile, out, ["--check-output"]);
    assert.equal(mutated.verdict.status, "rejected", mutated.log);
    assert(mutated.verdict.reason, JSON.stringify(mutated.verdict));
  }
}

// The provider's own import graph is discovered, copied beside the split and
// checked as part of the output. The extra module remains a real relative import.
{
  const dir = path.join(root, "transitive-provider");
  const files = prepare(dir, {
    source: program([
      importNamed("./provider.mjs", [["create", "make"]]),
      variable("const", "loader", call(id("make"), [arrow([], id("moved"))])),
      variable("const", "moved", arrow([], literal(83))),
      exportNamed([["loader", "result"]]),
    ]),
    provider: program([
      importNamed("./extra.mjs", [["run", "invoke"]]),
      variable("var", "factory", arrow([id("callback")], arrow([], call(id("invoke"), [id("callback")])))),
      exportNamed([["factory", "create"]]),
    ]),
    extras: {
      "extra.mjs": program([
        variable("var", "run", arrow([id("callback")], call(id("callback")))),
        exportNamed([["run", "run"]]),
      ]),
    },
    spec: {
      entry: "entry.mjs",
      order: ["fragment.mjs"],
      sequential: true,
      modules: { "entry.mjs": ["#0", "#1", "#3"], "fragment.mjs": ["#2"] },
    },
  });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  assert.equal(verdict.status, "certified", log);
  assert(existsSync(path.join(out, "provider.mjs")));
  assert(existsSync(path.join(out, "extra.mjs")));
  assert.deepEqual(observe(files.sourceFile, "result"), { result: 83 });
  assert.deepEqual(observe(path.join(out, "entry.mjs"), "result"), { result: 83 });
  assert.equal(lower(files.sourceFile, files.specFile, out, ["--check-output"]).verdict.status, "certified");
}

// Provider graph discovery refuses both a provider edge back to the input
// source and a symlink alias for a module, which would make output links
// ambiguous relative to the generated directory.
{
  const dir = path.join(root, "provider-request-back-to-source");
  const files = prepare(dir, {
    source: program([
      importNamed("./provider.mjs", [["create", "make"]]),
      variable("const", "marker", literal(9)),
      variable("const", "loader", call(id("make"), [arrow([], literal(1))])),
      exportNamed([
        ["marker", "marker"],
        ["loader", "result"],
      ]),
    ]),
    provider: program([
      importNamed("./source.mjs", [["marker", "sourceMarker"]]),
      variable("var", "factory", arrow([id("callback")], arrow([], call(id("callback"))))),
      exportNamed([["factory", "create"]]),
    ]),
    spec: {
      entry: "entry.mjs",
      order: [],
      sequential: true,
      modules: { "entry.mjs": ["#0", "#1", "#2", "#3"] },
    },
  });
  const out = path.join(dir, "out");
  const refused = lower(files.sourceFile, files.specFile, out);
  assert.equal(refused.verdict.status, "rejected", refused.log);
  assert.match(refused.verdict.reason, /request back to the source entry/i);
  assert(!existsSync(out));
}
{
  const dir = path.join(root, "provider-symlink-alias");
  const files = prepare(dir, {
    source: program([
      importNamed("./provider-alias.mjs", [["create", "make"]]),
      variable("const", "loader", call(id("make"), [arrow([], literal(1))])),
      exportNamed([["loader", "result"]]),
    ]),
    provider: program([
      variable("var", "factory", arrow([id("callback")], arrow([], call(id("callback"))))),
      exportNamed([["factory", "create"]]),
    ]),
    spec: {
      entry: "entry.mjs",
      order: [],
      sequential: true,
      modules: { "entry.mjs": ["#0", "#1", "#2"] },
    },
  });
  symlinkSync(files.providerFile, path.join(dir, "provider-alias.mjs"));
  const out = path.join(dir, "out");
  const refused = lower(files.sourceFile, files.specFile, out);
  assert.equal(refused.verdict.status, "rejected", refused.log);
  assert.match(refused.verdict.reason, /unsupported module alias/i);
  assert(!existsSync(out));
}

const eager = eagerProvider();

// An eager callback that sees the later const would read it in the TDZ in the
// source module. Moving that const into a dependency would initialize it first,
// so normal-completion certification must refuse this layout.
{
  const dir = path.join(root, "eager-observes-later-binding");
  const fragment = "fragment.mjs";
  const files = prepare(dir, {
    provider: eager,
    source: program([
      importNamed("./provider.mjs", [["create", "make"]]),
      variable("const", "loader", call(id("make"), [arrow([], id("moved"))])),
      variable("const", "moved", arrow([], literal(7))),
      exportNamed([["loader", "result"]]),
    ]),
    spec: {
      entry: "entry.mjs",
      order: [fragment],
      sequential: true,
      modules: { "entry.mjs": ["#0", "#1", "#3"], [fragment]: ["#2"] },
    },
  });
  assert.deepEqual(observe(files.sourceFile, "result"), { error: "ReferenceError" });

  // This is the schedule a successful move would publish: imports evaluate the
  // moved declaration before the entry invokes the eager factory.
  const counterfactual = path.join(dir, "counterfactual");
  mkdirSync(counterfactual, { recursive: true });
  writeFileSync(path.join(counterfactual, "provider.mjs"), eager);
  writeFileSync(
    path.join(counterfactual, fragment),
    program([variable("const", "moved", arrow([], literal(7))), exportNamed([["moved", "moved"]])])
  );
  writeFileSync(
    path.join(counterfactual, "entry.mjs"),
    program([
      importNamed("./provider.mjs", [["create", "make"]]),
      importNamed(`./${fragment}`, [["moved", "moved"]]),
      variable("const", "loader", call(id("make"), [arrow([], id("moved"))])),
      exportNamed([["loader", "result"]]),
    ])
  );
  assert.deepEqual(observe(path.join(counterfactual, "entry.mjs"), "result"), { result: 7 });

  const out = path.join(dir, "out");
  const refused = lower(files.sourceFile, files.specFile, out);
  assert.equal(refused.verdict.status, "rejected", refused.log);
  assert(!existsSync(out), "a behavior-changing eager provider move publishes no modules");
}

// The linked-module decoder rejects implicit `arguments` references: CoreJS
// does not allocate the ordinary function's arguments object.
{
  const dir = path.join(root, "implicit-arguments");
  const files = prepare(dir, {
    source: program([
      importNamed("./provider.mjs", [["create", "make"]]),
      variable("const", "loader", call(id("make"), [arrow([], literal(1))])),
      exportNamed([["loader", "result"]]),
    ]),
    provider: program([
      variable("var", "factory", functionExpression([id("callback")], block([returns(arrow([], id("arguments")))]))),
      exportNamed([["factory", "create"]]),
    ]),
    spec: {
      entry: "entry.mjs",
      order: [],
      sequential: true,
      modules: { "entry.mjs": ["#0", "#1", "#2"] },
    },
  });
  const out = path.join(dir, "out");
  const refused = lower(files.sourceFile, files.specFile, out);
  assert.equal(refused.verdict.status, "rejected", refused.log);
  assert.match(refused.verdict.reason, /unsupported implicit arguments reference/i);
  assert(!existsSync(out));
}
