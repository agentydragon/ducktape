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
const conditional = (test, consequent, alternate) => ({
  type: "ConditionalExpression",
  test,
  consequent,
  alternate,
});
const expression = (expression) => ({ type: "ExpressionStatement", expression });
const returns = (argument) => ({ type: "ReturnStatement", argument });
const throws = (argument) => ({ type: "ThrowStatement", argument });
const block = (body) => ({ type: "BlockStatement", body });
const tryStatement = (body, handler = null, finalizer = null) => ({
  type: "TryStatement",
  block: body,
  handler,
  finalizer,
});
const catchClause = (param, body) => ({ type: "CatchClause", param, body });
const arrow = (params, body) => ({
  type: "ArrowFunctionExpression",
  params,
  body,
  expression: body.type !== "BlockStatement",
});
const functionExpression = (params, body) => ({ type: "FunctionExpression", id: null, params, body });
const variables = (kind, bindings) => ({
  type: "VariableDeclaration",
  kind,
  declarations: bindings.map(([name, init]) => ({ type: "VariableDeclarator", id: id(name), init })),
});
const variable = (kind, name, init) => variables(kind, [[name, init]]);
const property = (key, value) => ({
  type: "Property",
  key: id(key),
  value,
  kind: "init",
  method: false,
  shorthand: false,
  computed: false,
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
  const result = spawnSync(process.env.LOWERSPLIT, [source, spec, out, ...extra], {
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

function generatedTryCase(seed) {
  switch (seed % 4) {
    case 0: {
      const value = seed + 20;
      return {
        body: [
          tryStatement(
            block([
              variables(seed === 0 ? "let" : "const", [
                ["first", literal(value)],
                ["second", id("first")],
              ]),
              throws(id("second")),
            ]),
            catchClause(id("caught"), block([returns(id("caught"))]))
          ),
        ],
        expectedValue: value,
      };
    }
    case 1:
      return {
        body: [
          tryStatement(block([throws(literal(seed + 30))]), catchClause(null, block([returns(literal(seed + 21))]))),
        ],
        expectedValue: seed + 21,
      };
    case 2: {
      const value = seed + 20;
      return {
        body: [
          variables("var", [
            ["readLater", arrow([], id("later"))],
            ["later", literal(value)],
            ["local", call(id("readLater"))],
            ["local", null],
          ]),
          tryStatement(
            block([throws(id("local"))]),
            catchClause(
              id("caught"),
              block([block([variable("const", "nested", id("caught")), returns(id("nested"))])])
            )
          ),
        ],
        expectedValue: value,
      };
    }
    case 3:
      return {
        body: [tryStatement(block([returns(literal(seed + 20))]), null, block([returns(literal(seed + 24))]))],
        expectedValue: seed + 24,
      };
    default:
      assert.fail(`unexpected generated try/catch seed ${seed}`);
  }
}

function generatedCase(seed) {
  const publicFactory = `create${seed}`;
  const sameValueFactory = `sameValue${seed}`;
  const localFactory = `imported${seed}`;
  const resultName = `publicResult${seed}`;
  const plainResultName = `plainResult${seed}`;
  const tryResultName = `tryResult${seed}`;
  const loaderName = `loader${seed}`;
  const movedName = `moved${seed}`;
  const tryCallableName = `tryCallable${seed}`;
  const ballastName = `ballast${seed}`;
  const plainName = `plain${seed}`;
  const fragment = `fragment-${seed}.mjs`;
  const providerMode = seed % 3;
  const hasSecondMovedDeclaration = seed % 2 === 1;
  const generatedTry = generatedTryCase(seed);
  const callback = providerMode === 0 ? arrow([], id(movedName)) : arrow([], literal(seed + 70 + providerMode));
  const sourceBody = [
    importNamed("./provider.mjs", [[publicFactory, localFactory]]),
    variable("const", ballastName, literal(seed)),
    variable(seed % 2 === 0 ? "const" : "var", loaderName, call(id(localFactory), [callback])),
    variable("const", movedName, arrow([], literal(seed + 20))),
    variable("const", tryCallableName, arrow([], block(generatedTry.body))),
    ...(hasSecondMovedDeclaration ? [variable("const", `fragmentValue${seed}`, literal(seed + 40))] : []),
    variable("const", plainName, arrow([], literal(seed + 60))),
    variable("const", tryResultName, call(id(tryCallableName))),
    exportNamed([
      [loaderName, resultName],
      [plainName, plainResultName],
      [tryResultName, tryResultName],
    ]),
  ];
  const movedIndex = 3;
  const tryIndex = 4;
  const extraIndex = 5;
  const plainIndex = hasSecondMovedDeclaration ? 6 : 5;
  const exportedIndex = sourceBody.length - 1;
  const fragmentIndices = [movedIndex, tryIndex, ...(hasSecondMovedDeclaration ? [extraIndex] : [])];
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
    tryResultName,
    providerMode,
    expectedValue: providerMode === 0 ? seed + 20 : providerMode === 1 ? seed + 71 : seed + 72,
    expectedTryValue: generatedTry.expectedValue,
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

// Move the worker to its own module and call it during initialization, so both
// certification and the Node observation exercise its body.
function workerSplit(name, source) {
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source,
    provider: "",
    spec: { entry: "entry.mjs", modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1"] }, order: ["worker.mjs"] },
  });
  const out = path.join(dir, "out");
  return { files, out, ...lower(files.sourceFile, files.specFile, out) };
}

function assertWorkerResult(name, source, expected) {
  const { files, out, verdict, log } = workerSplit(name, source);
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.guarantee, "normal-completion", log);
  for (const file of [files.sourceFile, path.join(out, "entry.mjs")]) {
    assert.deepEqual(observe(file, "result"), { result: expected }, name);
  }
}

function workerProgram(body) {
  return program([
    variable("const", "worker", arrow([], body)),
    {
      type: "ExportNamedDeclaration",
      declaration: variable("const", "result", call(id("worker"))),
      specifiers: [],
      source: null,
    },
  ]);
}

for (const [seed, value] of [0, "taken", "", 3].entries()) {
  const selected = literal(seed + 30);
  const skipped = call(arrow([], block([throws(literal(seed))])));
  const choose = conditional(literal(value), value ? selected : skipped, value ? skipped : selected);
  const source = workerProgram(choose);
  assertWorkerResult(`generated-conditional-${seed}`, source, seed + 30);
}

// The temporal dead zone throws from the test and from the selected arm respectively.
// Returning normally through either expression would bypass the catch.
for (const [name, expr] of [
  ["test", "later ? 1 : 2"],
  ["arm", "true ? later : 2"],
]) {
  const source = `const worker = () => {
    try { return ${expr}; let later; } catch { return 7; }
  };
  export const result = worker();`;
  assertWorkerResult(`conditional-abrupt-${name}`, source, 7);
}

// Generate both operand orders and equality outcomes.
for (const [index, [operand, tag, expected]] of [
  [id("undefined"), "undefined", false],
  [arrow([], literal(0)), "object", true],
  [literal(null), "object", false],
  [literal(3), "string", true],
].entries()) {
  const type = { type: "UnaryExpression", operator: "typeof", prefix: true, argument: operand };
  const [left, right] = index % 2 === 0 ? [literal(tag), type] : [type, literal(tag)];
  const expr = { type: "BinaryExpression", operator: "!=", left, right };
  assertWorkerResult(`generated-string-inequality-${index}`, workerProgram(expr), expected);
}

// Different numeric spellings compare by string contents.
assertWorkerResult(
  "unequal-numeric-strings",
  'const worker = () => "01" != "1"; export const result = worker();',
  true
);
assertWorkerResult(
  "typeof-local-tdz",
  'const worker = () => { try { return "undefined" != typeof later; let later; } catch { return 7; } }; export const result = worker();',
  7
);
assertWorkerResult(
  "typeof-shadowed-host-name",
  'const worker = (window) => "undefined" != typeof window; export const result = worker(null);',
  true
);
for (const [name, expr, reason] of [
  ["mixed-inequality", '"01" != 1', /requires two string operands/],
  ["host-typeof", '"undefined" != typeof window', /unsupported global window/],
]) {
  const { verdict, out } = workerSplit(name, `const worker = () => ${expr}; export const result = worker();`);
  assert.equal(verdict.status, "rejected");
  assert.match(verdict.reason, reason);
  assert(!existsSync(out));
}

// Generated aliases, layouts and provider-body shapes exercise the ordinary
// source-file CLI. The entry calls a moved try-body function during initialization.
for (let seed = 0; seed < 8; seed++) {
  const fixture = generatedCase(seed);
  const dir = path.join(root, `generated-${seed}`);
  const files = prepare(dir, fixture);
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  const exportNames = [fixture.resultName, fixture.plainResultName, fixture.tryResultName];
  const original = observe(files.sourceFile, exportNames);
  assert.deepEqual(
    original,
    {
      [fixture.resultName]: fixture.expectedValue,
      [fixture.plainResultName]: seed + 60,
      [fixture.tryResultName]: fixture.expectedTryValue,
    },
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
    writeFileSync(entryFile, `// formatting outside callables is immaterial\n${entrySource}`);
    const formatting = lower(files.sourceFile, files.specFile, out, ["--check-output"]);
    assert.equal(formatting.verdict.status, "certified", formatting.log);
    writeFileSync(entryFile, entrySource);

    const callable = entrySource.match(/\(\)\s*=>\s*moved0/)?.[0];
    assert(callable, entrySource);
    const alternateCallable = callable.includes("=> ")
      ? callable.replace("=> ", "=>  ")
      : callable.replace("=>", " =>");
    assert.notEqual(alternateCallable, callable);
    writeFileSync(entryFile, entrySource.replace(callable, alternateCallable));
    const callableMutation = lower(files.sourceFile, files.specFile, out, ["--check-output"]);
    assert.equal(callableMutation.verdict.status, "rejected", callableMutation.log);
    writeFileSync(entryFile, entrySource);

    const link = new RegExp(`\\b${fixture.publicFactory}(?=\\s+as\\s+${fixture.localFactory}\\b)`);
    assert.match(entrySource, link);
    writeFileSync(entryFile, entrySource.replace(link, fixture.sameValueFactory));
    const mutated = lower(files.sourceFile, files.specFile, out, ["--check-output"]);
    assert.equal(mutated.verdict.status, "rejected", mutated.log);
    assert(mutated.verdict.reason, JSON.stringify(mutated.verdict));
  }
}

// The second lexical binding shadows the function parameter throughout the try block.
{
  const source = `const run = (later) => {
    try { let first = later, later = 9; }
    catch { return true; }
    throw false;
  };
  export const observed = run(3);`;
  const files = prepare(path.join(root, "group-hoisting-and-tdz"), {
    source,
    provider: "",
    spec: { entry: "entry.mjs", modules: { "worker.mjs": ["#0"], "entry.mjs": ["#1"] }, order: ["worker.mjs"] },
  });
  const out = path.join(root, "group-hoisting-and-tdz", "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  assert.equal(verdict.status, "certified", log);
  assert.deepEqual(observe(files.sourceFile, "observed"), { observed: true });
  assert.deepEqual(observe(path.join(out, "entry.mjs"), "observed"), { observed: true });
}

// The catch binding is visible inside its handler, including a nested block.
// A normal callback skips the handler and still reads the function parameter.
{
  const dir = path.join(root, "linked-function-catch-scope");
  const fragment = "worker.mjs";
  const sourceBody = [
    variable(
      "const",
      "worker",
      functionExpression(
        [id("callback"), id("value")],
        block([
          tryStatement(
            block([expression(call(id("callback")))]),
            catchClause(id("value"), block([block([variable("const", "nested", id("value")), returns(id("nested"))])]))
          ),
          returns(id("value")),
        ])
      )
    ),
    variable("const", "caughtResult", call(id("worker"), [arrow([], block([throws(literal(17))])), literal(9)])),
    variable("const", "normalResult", call(id("worker"), [arrow([], block([])), literal(9)])),
    exportNamed([
      ["caughtResult", "caughtResult"],
      ["normalResult", "normalResult"],
    ]),
  ];
  const files = prepare(dir, {
    source: program(sourceBody),
    provider: program([]),
    spec: {
      entry: "entry.mjs",
      order: [fragment],
      sequential: true,
      modules: { "entry.mjs": ["#1", "#2", "#3"], [fragment]: ["#0"] },
    },
  });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  assert.equal(verdict.status, "certified", log);
  const original = observe(files.sourceFile, ["caughtResult", "normalResult"]);
  assert.deepEqual(original, { caughtResult: 17, normalResult: 9 });
  assert.deepEqual(observe(path.join(out, "entry.mjs"), ["caughtResult", "normalResult"]), original);
}

// Destructuring catch parameters remain outside this lowering's supported
// subset, even though the source itself is valid and has a normal result.
{
  const dir = path.join(root, "unsupported-catch-pattern");
  const fragment = "worker.mjs";
  const catchPattern = {
    type: "ObjectPattern",
    properties: [property("value", id("value"))],
  };
  const sourceBody = [
    variable(
      "const",
      "worker",
      functionExpression(
        [],
        block([tryStatement(block([throws(literal(1))]), catchClause(catchPattern, block([returns(literal(7))])))])
      )
    ),
    variable("const", "result", call(id("worker"))),
    exportNamed([["result", "result"]]),
  ];
  const files = prepare(dir, {
    source: program(sourceBody),
    provider: program([]),
    spec: {
      entry: "entry.mjs",
      order: [fragment],
      sequential: true,
      modules: { "entry.mjs": ["#1", "#2"], [fragment]: ["#0"] },
    },
  });
  assert.deepEqual(observe(files.sourceFile, "result"), { result: 7 });
  const out = path.join(dir, "out");
  const refused = lower(files.sourceFile, files.specFile, out);
  assert.equal(refused.verdict.status, "rejected", refused.log);
  assert.match(refused.verdict.reason ?? "", /expected an identifier/i, refused.log);
  assert(!existsSync(out), "an unsupported catch pattern publishes no modules");
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
