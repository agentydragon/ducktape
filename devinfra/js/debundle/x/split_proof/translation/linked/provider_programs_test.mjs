import assert from "node:assert/strict";
import { existsSync, mkdirSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  assertCertified,
  assertRejectedOrOrderKeptSlice,
  caseShard,
  root,
  id,
  literal,
  call,
  expression,
  returns,
  throws,
  block,
  tryStatement,
  catchClause,
  arrow,
  functionExpression,
  variables,
  variable,
  property,
  importNamed,
  exportNamed,
  program,
  observe,
  lower,
  prepare,
} from "./harness.mjs";

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
  const reexportedName = `publicMoved${seed}`;
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
      [hasSecondMovedDeclaration ? `fragmentValue${seed}` : movedName, reexportedName],
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
    reexportedName,
    expectedReexport: seed + (hasSecondMovedDeclaration ? 40 : 20),
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

const runCase = caseShard();

// The entry publishes bindings from both a copied provider and a newly split module.
if (runCase("entry-exports-imports")) {
  const dir = path.join(root, "entry-exports-imports");
  const source = `
    import { original as imported } from "./provider.mjs";
    const moved = () => imported;
    export { imported as value, moved as getValue };
  `;
  const files = prepare(dir, {
    source,
    provider: "export const original = 31;",
    spec: { entry: "entry.mjs", modules: { "worker.mjs": ["#1"], "entry.mjs": ["#0", "#2"] }, order: ["worker.mjs"] },
  });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  assertCertified(verdict, log);
  for (const file of [files.sourceFile, path.join(out, "entry.mjs")]) {
    const namespace = await import(pathToFileURL(file).href);
    assert.deepEqual(Object.keys(namespace), ["getValue", "value"]);
    assert.equal(namespace.value, 31);
    assert.equal(namespace.getValue(), 31);
  }
}

// Generated aliases, layouts and provider-body shapes exercise the ordinary
// source-file CLI. The entry calls a moved try-body function during initialization.
for (let seed = 0; seed < 8; seed++) {
  if (!runCase(`generated-${seed}`)) continue;
  const fixture = generatedCase(seed);
  const dir = path.join(root, `generated-${seed}`);
  const files = prepare(dir, fixture);
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  const exportNames = [fixture.resultName, fixture.plainResultName, fixture.tryResultName, fixture.reexportedName];
  const original = observe(files.sourceFile, exportNames);
  assert.deepEqual(
    original,
    {
      [fixture.resultName]: fixture.expectedValue,
      [fixture.plainResultName]: seed + 60,
      [fixture.tryResultName]: fixture.expectedTryValue,
      [fixture.reexportedName]: fixture.expectedReexport,
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
if (runCase("group-hoisting-and-tdz")) {
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
if (runCase("linked-function-catch-scope")) {
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
if (runCase("unsupported-catch-pattern")) {
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
  const orderKept = assertRejectedOrOrderKeptSlice({ ...refused, out }, /expected an identifier/i);
  if (orderKept) {
    assert.deepEqual(observe(path.join(out, "entry.mjs"), "result"), { result: 7 });
  }
}

// The provider's own import graph is discovered, copied beside the split and
// checked as part of the output. The extra module remains a real relative import.
if (runCase("transitive-provider")) {
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
if (runCase("provider-request-back-to-source")) {
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
if (runCase("provider-symlink-alias")) {
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
if (runCase("eager-observes-later-binding")) {
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

// The provider's returned arrow keeps the arguments object of its creating call.
if (runCase("implicit-arguments")) {
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
  const run = lower(files.sourceFile, files.specFile, out);
  assertCertified(run.verdict, run.log);
  for (const file of [files.sourceFile, path.join(out, "entry.mjs")]) {
    const { result } = await import(pathToFileURL(file).href);
    assert.equal(result().length, 1);
    assert.equal(result()[0](), 1);
  }
}
