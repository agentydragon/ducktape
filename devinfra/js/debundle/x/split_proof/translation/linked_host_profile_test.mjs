import assert from "node:assert/strict";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  arrow,
  block,
  call,
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
  variable,
} from "./linked_programs_test_support.mjs";

const profile = path.join(root, "page.json");
writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
const entry = "entry.mjs";
function fixture(name, source) {
  const dir = path.join(root, name);
  const files = prepare(dir, {
    source,
    provider: "",
    spec: { entry, modules: { "worker.mjs": ["#0"], [entry]: ["#1", "#2"] }, order: ["worker.mjs"], sequential: true },
  });
  const out = path.join(dir, "out");
  return { ...files, out, run: (...opts) => lower(files.sourceFile, files.specFile, out, opts).verdict };
}

for (const [binding, defaulted] of [
  ["ambient", false],
  ["local", false],
  ["ambient", true],
  ["local", true],
]) {
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
    defaulted ? `${binding}-default` : binding,
    program([
      variable("const", "read", read),
      variable("const", "result", call(id("read"), local ? [literal(42)] : [])),
      exportNamed([["result", "result"]]),
    ])
  );
  if (!local) {
    assert.match(test.run().reason, /unsupported global window/);
    assert(!existsSync(test.out));
  }
  const verdict = test.run("--host-contract", profile);
  assert.equal(verdict.status, "certified", JSON.stringify(verdict));
  assert.equal(verdict.hostProfile, "browser-page");
  assert.equal(verdict.assumptions.length, local ? 0 : 1);
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

for (const [name, body, reason] of [
  ["bare", "return window;", /unsupported global window/],
  ["property", "return typeof window.document;", /unsupported global window/],
  ["other-global", "return typeof document;", /unsupported global document/],
  ["tdz", "return typeof window; let window;", /threw during linked initialization/],
]) {
  const test = fixture(name, `const read = () => { ${body} }; const result = read(); export { result };`);
  assert.match(test.run("--host-contract", profile).reason, reason);
  assert(!existsSync(test.out));
}

for (const args of [[], [literal("absent")]]) {
  for (const reached of [false, true]) {
    const construction = { type: "NewExpression", callee: id("Error"), arguments: args };
    const source = program([
      variable("const", "read", arrow([id("fail")], conditional(id("fail"), construction, literal(42)))),
      variable("const", "result", call(id("read"), [literal(reached)])),
      exportNamed([["result", "result"]]),
    ]);
    const test = fixture(`opaque-error-${args.length}-${reached}`, source);
    assert.match(test.run().reason, /unsupported global Error/);
    const verdict = test.run("--host-contract", profile);
    if (reached) {
      assert.match(verdict.reason, /unknown.*ambient constructor Error is reached/);
      assert(!existsSync(test.out));
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
const caught = fixture(
  "caught-opaque-error",
  'const read = () => { try { throw new Error((() => { throw "argument"; })()); } catch { return 7; } }; const result = read(); export { result };'
);
assert.match(caught.run("--host-contract", profile).reason, /unknown.*ambient constructor Error is reached/);
assert(!existsSync(caught.out));

const lexical = fixture(
  "shadowed-error-constructor",
  'const read = () => { class Error { value = "local"; } return (new Error()).value; }; const result = read(); export { result };'
);
const localVerdict = lexical.run("--host-contract", profile);
assert.equal(localVerdict.status, "certified", JSON.stringify(localVerdict));
assert.deepEqual(localVerdict.assumptions, []);
for (const file of [lexical.sourceFile, path.join(lexical.out, entry)]) {
  assert.equal((await import(pathToFileURL(file).href)).result, "local");
}

// Both constructs occur in the same program. The crossing is valid in a page.
// In a realm without window, it reaches the deferred constructor in the split only.
for (const pageSkipsError of [true, false]) {
  const scope = path.join(root, `profile-reversal-${pageSkipsError}`);
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
