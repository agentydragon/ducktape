import assert from "node:assert/strict";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { split } from "../testing/split.mjs";
import {
  assertCertified,
  arrow,
  assertRefusedOrOrderKeptSlice,
  block,
  call,
  caseShard,
  conditional,
  exportNamed,
  expression,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  prepare,
  program,
  returns,
  root,
  throws,
  threeUnitFixture,
  variable,
} from "./harness.mjs";

const profile = path.join(root, "page.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const nodeProfile = path.join(root, "node-recording.json");
writeFileSync(nodeProfile, JSON.stringify({ profile: "node-recording" }));
const entry = "entry.mjs";
const includesCase = caseShard();
const fixture = (name, source) => threeUnitFixture(name, source, entry, true);

const nodeProfileFixture = fixture(
  "unsupported-node-recording-profile",
  "const moved = 1; const kept = 2; export const result = moved + kept;"
);
const unsupportedProfile = split({
  source: nodeProfileFixture.sourceFile,
  spec: nodeProfileFixture.specFile,
  out: nodeProfileFixture.out,
  hostContract: nodeProfile,
});
assert.equal(unsupportedProfile.status, "refused", unsupportedProfile.log);
assert.match(unsupportedProfile.reason, /unsupported host contract/);
assert(!existsSync(nodeProfileFixture.out));

for (const [binding, defaulted] of [
  ["ambient", false],
  ["local", false],
  ["ambient", true],
  ["local", true],
]) {
  const name = defaulted ? `${binding}-default` : binding;
  if (!includesCase(name)) continue;
  const local = binding === "local";
  const parameters = local ? [id("window")] : [];
  const type = {
    type: "UnaryExpression",
    prefix: true,
    operator: "typeof",
    argument: id("window"),
  };
  if (defaulted) parameters.push({ type: "AssignmentPattern", left: id("value"), right: type });
  const read = arrow(parameters, defaulted ? id("value") : type);
  const test = fixture(
    name,
    program([
      variable("const", "read", read),
      variable("const", "result", call(id("read"), local ? [literal(42)] : [])),
      exportNamed([["result", "result"]]),
    ])
  );
  if (!local) {
    const unprofiled = test.run();
    if (unprofiled.status === "certified") {
      assert.equal(unprofiled.scope, "linked-slice-order-kept", JSON.stringify(unprofiled));
      assert.equal(unprofiled.guarantee, "all-outcomes", JSON.stringify(unprofiled));
      assert(existsSync(test.out));
    } else {
      assert.match(unprofiled.reason, /unsupported global window/);
      assert(!existsSync(test.out));
    }
  }
  const verdict = test.run("--host-contract", profile);
  assert.equal(verdict.status, "certified", JSON.stringify(verdict));
  if (verdict.scope === "linked-slice-order-kept") assert.equal(verdict.guarantee, "all-outcomes");
  else assert.equal(verdict.scope, "linked-programs", JSON.stringify(verdict));
  assert.equal(verdict.hostProfile, "browser-page");
  assert.equal(verdict.assumptions.length, verdict.scope === "linked-slice-order-kept" || local ? 0 : 1);
  assert.deepEqual(test.run("--host-contract", profile, "--check-output"), verdict);
  // Supply the single observation promised by the profile in this Node realm.
  globalThis.window = {};
  try {
    for (const file of [test.sourceFile, path.join(test.out, entry)]) {
      assert.equal((await import(pathToFileURL(file).href)).result, local ? "number" : "object");
    }
  } finally {
    delete globalThis.window;
  }
}

// Preserve the ambient symbol operations and the captured initializer across the split.
// The provider defers invoking the factory until after initialization.
if (includesCase("deferred-ambient-symbol")) {
  const dir = path.join(root, "deferred-ambient-symbol");
  const files = prepare(dir, {
    source: `
      import { defer } from "./provider.mjs";
      const load = defer(() => [Symbol.for(moved()), Symbol.iterator]);
      const moved = () => "fixture";
      export { load, moved };
    `,
    provider: "export const defer = (factory) => () => factory();",
    spec: {
      entry,
      modules: { [entry]: ["#0", "#1", "#3"], "worker.mjs": ["#2"] },
      order: ["worker.mjs"],
      sequential: true,
    },
  });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  assertCertified(verdict, log);
  assert.equal(verdict.hostProfile, undefined);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    const namespace = await import(pathToFileURL(file).href);
    assert.equal(namespace.moved(), "fixture");
    assert.deepEqual(namespace.load(), [Symbol.for("fixture"), Symbol.iterator]);
  }
}

if (includesCase("caught-opaque-symbol-for")) {
  const caughtSymbol = fixture(
    "caught-opaque-symbol-for",
    'const read = () => { try { return Symbol.for((() => { throw "argument"; })()); } catch { return "caught"; } }; const result = read(); export { result };'
  );
  assertRefusedOrOrderKeptSlice(caughtSymbol.run(), caughtSymbol.out, /unknown.*ambient member Symbol\.for is reached/);
}

if (includesCase("reached-symbol-iterator")) {
  const reachedIterator = fixture(
    "reached-symbol-iterator",
    "const read = () => Symbol.iterator; const result = read(); export { result };"
  );
  assertRefusedOrOrderKeptSlice(
    reachedIterator.run(),
    reachedIterator.out,
    /unknown.*ambient member Symbol\.iterator is reached/
  );
}

if (includesCase("deferred-ambient-object")) {
  const dir = path.join(root, "deferred-ambient-object");
  const files = prepare(dir, {
    source: `
      import { defer } from "./provider.mjs";
      const load = defer(function summarize(prefix) {
        return Object.assign({}, { label: prefix + moved() });
      });
      const moved = () => "fixture";
      export { load, moved };
    `,
    provider: "export const defer = (factory) => factory;",
    spec: {
      entry,
      modules: { [entry]: ["#0", "#1", "#3"], "worker.mjs": ["#2"] },
      order: ["worker.mjs"],
      sequential: true,
    },
  });
  const out = path.join(dir, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out);
  assertCertified(verdict, log);
  assert.deepEqual(lower(files.sourceFile, files.specFile, out, ["--check-output"]).verdict, verdict);
  for (const file of [files.sourceFile, path.join(out, entry)]) {
    const { load } = await import(pathToFileURL(file).href);
    assert.equal(load.name, "summarize");
    assert.equal(load.length, 1);
    assert.deepEqual(load("deferred "), { label: "deferred fixture" });
  }
}

if (includesCase("reached-retained-body")) {
  const reachedBody = fixture(
    "reached-retained-body",
    `const read = () => {
    const unopened = () => document.title;
    const called = () => Object.keys({});
    try { return called(); } catch { return "caught"; }
  };
  const result = read();
  export { result };`
  );
  assertRefusedOrOrderKeptSlice(
    reachedBody.run(),
    reachedBody.out,
    /unknown.*deferred function called: unsupported global Object/
  );
}

for (const args of [[], [literal("absent")]]) {
  for (const reached of [false, true]) {
    const name = `opaque-error-${args.length}-${reached}`;
    if (!includesCase(name)) continue;
    const construction = { type: "NewExpression", callee: id("Error"), arguments: args };
    const source = program([
      variable("const", "read", arrow([id("fail")], conditional(id("fail"), construction, literal(42)))),
      variable("const", "result", call(id("read"), [literal(reached)])),
      exportNamed([["result", "result"]]),
    ]);
    const test = fixture(name, source);
    assertRefusedOrOrderKeptSlice(test.run(), test.out, /unsupported global Error/);
    const verdict = test.run("--host-contract", profile);
    if (reached) {
      assertRefusedOrOrderKeptSlice(verdict, test.out, /unknown.*ambient constructor Error is reached/);
    } else {
      assert.equal(verdict.status, "certified", JSON.stringify(verdict));
      assert.deepEqual(verdict.assumptions, []);
      assert.deepEqual(test.run("--host-contract", profile, "--check-output"), verdict);
      for (const file of [test.sourceFile, path.join(test.out, entry)]) {
        assert.equal((await import(pathToFileURL(file).href)).result, 42);
      }
    }
  }
}

// A catch cannot turn unknown host construction into a proved normal return.
if (includesCase("caught-opaque-error")) {
  const caught = fixture(
    "caught-opaque-error",
    'const read = () => { try { throw new Error((() => { throw "argument"; })()); } catch { return 7; } }; const result = read(); export { result };'
  );
  assertRefusedOrOrderKeptSlice(
    caught.run("--host-contract", profile),
    caught.out,
    /unknown.*ambient constructor Error is reached/
  );
}

if (includesCase("shadowed-host-bindings")) {
  const lexical = fixture(
    "shadowed-host-bindings",
    `const read = () => {
    class Error { value = "local"; }
    const Symbol = { for: (key) => key };
    return Symbol.for((new Error()).value);
  };
  const result = read(); export { result };`
  );
  const localVerdict = lexical.run("--host-contract", profile);
  assert.equal(localVerdict.status, "certified", JSON.stringify(localVerdict));
  assert.deepEqual(localVerdict.assumptions, []);
  for (const file of [lexical.sourceFile, path.join(lexical.out, entry)]) {
    assert.equal((await import(pathToFileURL(file).href)).result, "local");
  }
}

// Both constructs occur in the same program. The crossing is valid in a page.
// In a realm without window, it reaches the deferred constructor in the split only.
for (const pageSkipsError of [true, false]) {
  const name = `profile-reversal-${pageSkipsError}`;
  if (!includesCase(name)) continue;
  const scope = path.join(root, name);
  const order = ["worker.mjs", "reader.mjs", "state.mjs"];
  const files = prepare(scope, {
    source: program([
      {
        type: "FunctionDeclaration",
        id: id("read"),
        params: [],
        generator: false,
        async: false,
        body: block([
          {
            type: "IfStatement",
            test: {
              type: "BinaryExpression",
              operator: pageSkipsError ? "===" : "!==",
              left: { type: "UnaryExpression", operator: "typeof", prefix: true, argument: id("window") },
              right: literal("object"),
            },
            consequent: returns(literal("page")),
            alternate: null,
          },
          {
            type: "IfStatement",
            test: { type: "BinaryExpression", operator: "===", left: id("current"), right: id("undefined") },
            consequent: throws({ type: "NewExpression", callee: id("Error"), arguments: [literal("too early")] }),
            alternate: null,
          },
          returns(id("current")),
        ]),
      },
      variable("var", "current", null),
      expression({ type: "AssignmentExpression", operator: "=", left: id("current"), right: literal("ready") }),
      variable("const", "result", call(id("read"))),
      exportNamed([["result", "result"]]),
    ]),
    provider: "",
    spec: {
      entry,
      modules: { "worker.mjs": ["#0"], "state.mjs": ["#1", "#2"], "reader.mjs": ["#3"], [entry]: ["#4"] },
      order,
      sequential: true,
    },
  });
  const out = path.join(scope, "out");
  const { verdict, log } = lower(files.sourceFile, files.specFile, out, ["--host-contract", profile]);
  if (!pageSkipsError) {
    assert.match(verdict.reason, /split reached an unknown result.*ambient constructor Error is reached/);
    assert(!existsSync(out));
    continue;
  }
  assert.equal(verdict.status, "certified", log);
  assert.equal(verdict.assumptions.length, 1);
  assert.deepEqual(moduleEvaluationOrder(out, entry), [...order, entry]);
  globalThis.window = {};
  try {
    for (const file of [files.sourceFile, path.join(out, entry)]) {
      assert.equal((await import(pathToFileURL(file).href)).result, "page");
    }
  } finally {
    delete globalThis.window;
  }
  assert.deepEqual(observe(files.sourceFile, "result"), { result: "ready" });
  assert.deepEqual(observe(path.join(out, entry), "result"), { error: "Error" });
}

for (const active of [false, true]) {
  const name = `page-url-${active}`;
  if (!includesCase(name)) continue;
  const useURL = {
    type: "NewExpression",
    callee: id("URL"),
    arguments: [literal("https://example.org/")],
  };
  const test = fixture(
    name,
    program([
      variable("const", "make", arrow([id("parse")], conditional(id("parse"), useURL, literal("help")))),
      variable("const", "result", call(id("make"), [literal(active)])),
      exportNamed([
        ["make", "make"],
        ["result", "result"],
      ]),
    ])
  );
  const { status, reason, assumptions } = test.run("--host-contract", profile);
  if (active) {
    assert.equal(status, "rejected");
    assert.match(reason, /ambient constructor URL is reached/);
    assert(!existsSync(test.out));
  } else {
    assert.equal(status, "certified", reason);
    assert.deepEqual(assumptions, []);
    for (const file of [test.sourceFile, path.join(test.out, entry)]) {
      const mod = await import(pathToFileURL(file).href);
      assert.equal(mod.result, "help");
      assert.equal(mod.make(true).hostname, "example.org");
    }
  }
}
