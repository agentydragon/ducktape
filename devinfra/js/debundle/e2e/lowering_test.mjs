// End-to-end behavior-preservation tests for the debundler.
//
// Drives the run_transform CLI as a black-box binary through a JSONC spec and
// asserts on the emitted file tree. Reuse for behavioral parity testing
// across language ports — assertions only depend on the spec schema, file
// layout, and runtime semantics of the emitted JS.
import test from "node:test";
import {
  assertEntryOutput,
  assertModuleExports,
  assertModuleSource,
  expectLogicalModulesE2eRejection,
  logicalModule,
  runLogicalModulesE2eFixture,
} from "./support.mjs";

// --- Behavior preservation ------------------------------------------------

test("preserves source-order evaluation across split declarator fragments", async (t) => {
  // Single declaration with cross-fragment dep `c = a + b`. Selecting `c`
  // forces the closure to pull `a` and `b` into the module while `z` stays in
  // residual. Tests that fragment splitting respects intra-declaration order.
  const fixture = await runLogicalModulesE2eFixture(t, {
    source: `const a = 1, b = 2, c = a + b;
const z = "z";
console.log(c);
export { c, z };
`,
    operations: [logicalModule("x", [{ name: "c" }])],
  });
  assertModuleExports({ includes: ["c"], modulePath: "static/app/modules/x.js", outRoot: fixture.outRoot });
  assertModuleExports({
    excludes: ["c"],
    modulePath: "static/app/modules/residual/unhandled.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "3\n");
});

test("preserves function declaration hoisting across modules", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
    source: `function a() { return b(); }
const c = a();
function b() { return "b"; }
console.log(c);
export { c };
`,
    operations: [
      logicalModule("helper", [
        { name: "a", kind: "FunctionDeclaration" },
        { name: "b", kind: "FunctionDeclaration" },
      ]),
      logicalModule("consumer", [{ name: "c" }]),
    ],
  });
  assertModuleExports({ includes: ["a"], modulePath: "static/app/modules/helper.js", outRoot: fixture.outRoot });
  assertModuleExports({ includes: ["c"], modulePath: "static/app/modules/consumer.js", outRoot: fixture.outRoot });
  assertEntryOutput(fixture, "b\n");
});

test("preserves default references after readable and explicit renames", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
    source: `const q = () => "a";
const b = ({ a: c = q } = {}) => c();
console.log(b({}), b({ a: () => "b" }));
export { q, b };
`,
    operations: [
      logicalModule("x", [
        { name: "alpha", binding: "q" },
        { name: "beta", binding: "b" },
      ]),
    ],
  });
  assertModuleExports({ includes: ["alpha", "beta"], modulePath: "static/app/modules/x.js", outRoot: fixture.outRoot });
  assertEntryOutput(fixture, "a b\n");
});

test("extracts a class declaration without changing runtime", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
    source: `class A { static label() { return "a"; } }
function b() { return A.label(); }
console.log(b());
export { A, b };
`,
    operations: [
      logicalModule("x", [
        { name: "A", kind: "ClassDeclaration" },
        { name: "b", kind: "FunctionDeclaration" },
      ]),
    ],
  });
  assertModuleSource({
    matches: [/\bclass A\b/, /\bfunction b\(\)/],
    modulePath: "static/app/modules/x.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "a\n");
});

test("lowers TS-enum-style self-referencing var declarations correctly", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
    source: `var A = ((B) => { B.X = "x"; B.Y = "y"; return B; })(A || {});
function b() { return A.X; }
console.log(b());
export { b };
`,
    operations: [logicalModule("x", [{ name: "A" }, { name: "b", kind: "FunctionDeclaration" }])],
  });
  assertModuleSource({
    matches: [/\bvar A = \(B =>/, /\bfunction b\(\)/],
    modulePath: "static/app/modules/x.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "x\n");
});

// --- Module structure: plain-import vs. init-wrapper ----------------------

test("emits a plain import without an init wrapper for a pure module", async (t) => {
  // The runtime side effect references `c` (residual), not the extracted
  // bindings, so nothing gets attached to the module's init wrapper.
  const fixture = await runLogicalModulesE2eFixture(t, {
    source: `const a = 1;
function b() { return a; }
const c = b();
console.log(c);
export { c };
`,
    operations: [logicalModule("x", [{ name: "a" }, { name: "b", kind: "FunctionDeclaration" }])],
  });
  assertModuleSource({
    matches: [/\bconst a = 1;/, /\bfunction b\(\)/],
    doesNotMatch: [/__dt_generated_init__x\b/],
    modulePath: "static/app/modules/x.js",
    outRoot: fixture.outRoot,
  });
  assertModuleSource({
    doesNotMatch: [/__dt_generated_init__x\b/],
    modulePath: "static/app/entry.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "1\n");
});

test("emits an init wrapper when the extracted module has top-level effects", async (t) => {
  // The initializer of `a` has a side effect (the comma expression), forcing
  // the extractor to emit an init wrapper rather than a plain const.
  const fixture = await runLogicalModulesE2eFixture(t, {
    source: `globalThis.log = "";
const a = (globalThis.log += "a", 1);
console.log(globalThis.log, a);
export { a };
`,
    operations: [logicalModule("x", [{ name: "a" }])],
  });
  assertModuleSource({
    matches: [/export function __dt_generated_init__x/],
    modulePath: "static/app/modules/x.js",
    outRoot: fixture.outRoot,
  });
  assertModuleSource({
    matches: [/__dt_generated_init__x\(\);/],
    modulePath: "static/app/entry.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "a 1\n");
});

// --- Rejections -----------------------------------------------------------

test("rejects extraction with a propagated final-name collision", async (t) => {
  // Two members both renamed to "a" — one from the variable `a`, one from
  // function `b`. The extractor should refuse before emitting.
  await expectLogicalModulesE2eRejection(t, {
    source: `const a = 1;
function b() { return a; }
console.log(b());
export { b };
`,
    operations: [logicalModule("x", [{ name: "a" }, { name: "a", binding: "b", kind: "FunctionDeclaration" }])],
    errorPattern:
      /propagated final name collision|conflicts with existing top-level binding|duplicate binding name|duplicate exported logical names/i,
  });
});
