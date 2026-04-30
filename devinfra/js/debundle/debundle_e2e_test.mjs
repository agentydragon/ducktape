// End-to-end tests treating the debundler as a black-box binary.
//
// Each test writes a synthetic JS bundle, runs the run_transform CLI through
// a JSONC spec, and asserts on the emitted file tree. Reuse this file for
// behavioral parity testing across language ports — assertions only depend on
// the spec schema, file layout, and runtime semantics of the emitted JS.
import test from "node:test";
import {
  assertEntryOutput,
  assertGeneratedModuleAfterEntryScript,
  assertModuleExports,
  assertModuleSource,
  expectLogicalModulesE2eRejection,
  runLogicalModulesE2eFixture,
} from "./pipeline_e2e_support.mjs";

function logicalModule(path, members) {
  return {
    id: `logical__${path.replace(/\//g, "_")}`,
    operation: "define_logical_module",
    selector: { chunkId: "static/app" },
    target: { path },
    members: members.map(({ name, kind = "VariableDeclarator", binding = name, alias }) => ({
      id: `member__${name}`,
      name: alias ?? name,
      selector: { binding: { kind, name: binding } },
    })),
  };
}

// --- Behavior preservation ------------------------------------------------

test("preserves source-order evaluation across split declarator fragments", async () => {
  // Single declaration with cross-fragment dep `c = a + b`. Selecting `c`
  // forces the closure to pull `a` and `b` into the module while `z` stays in
  // residual. Tests that fragment splitting respects intra-declaration order.
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-split-declarator-order-",
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

test("preserves function declaration hoisting across modules", async () => {
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-hoisted-functions-",
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

test("preserves default references after readable and explicit renames", async () => {
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-default-reference-rename-",
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

test("extracts a class declaration without changing runtime", async () => {
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-class-declaration-",
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

test("lowers TS-enum-style self-referencing var declarations correctly", async () => {
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-snapshot-variable-",
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

test("emits a plain import without an init wrapper for a pure module", async () => {
  // The runtime side effect references `c` (residual), not the extracted
  // bindings, so nothing gets attached to the module's init wrapper.
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-plain-import-",
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

test("emits an init wrapper when the extracted module has top-level effects", async () => {
  // The initializer of `a` has a side effect (the comma expression), forcing
  // the extractor to emit an init wrapper rather than a plain const.
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-init-wrapper-",
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

// --- Cross-module dependency wiring ---------------------------------------

test("closes an extracted module over its helper dependencies", async () => {
  // Selecting only `b`. Its helper `a` must be pulled into the module file
  // (as an internal binding, not exported) and removed from residual.
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-helper-closure-",
    source: `const a = x => "h:" + x;
const b = x => a(x);
console.log(b("y"));
export { b };
`,
    operations: [logicalModule("x", [{ name: "b" }])],
  });
  assertModuleExports({ includes: ["b"], modulePath: "static/app/modules/x.js", outRoot: fixture.outRoot });
  assertModuleSource({
    matches: [/\ba = x =>/],
    modulePath: "static/app/modules/x.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "h:y\n");
});

test("duplicates a shared bootstrap dependency into each named module", async () => {
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-shared-bootstrap-",
    source: `const q = "a";
function r() { return q; }
function s() { return "b" + r(); }
function t() { return s() + r(); }
console.log(t());
export { t, s };
`,
    operations: [
      logicalModule("inner", [{ name: "s", kind: "FunctionDeclaration" }]),
      logicalModule("outer", [{ name: "t", kind: "FunctionDeclaration" }]),
    ],
  });
  assertModuleExports({ includes: ["r", "s"], modulePath: "static/app/modules/inner.js", outRoot: fixture.outRoot });
  assertModuleExports({
    excludes: ["r", "s"],
    includes: ["t"],
    modulePath: "static/app/modules/outer.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "baa\n");
});

test("imports renamed dependencies across split declarators", async () => {
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-renamed-fragment-dependency-",
    source: `const q = o => o.a, r = o => o.b;
const s = o => q(o) ?? r(o);
console.log(s({ a: null, b: "c" }));
export { s };
`,
    operations: [
      logicalModule("provider", [
        { name: "u", binding: "q" },
        { name: "v", binding: "r" },
      ]),
      logicalModule("consumer", [{ name: "w", binding: "s" }]),
    ],
  });
  assertModuleExports({ includes: ["u", "v"], modulePath: "static/app/modules/provider.js", outRoot: fixture.outRoot });
  assertModuleExports({
    excludes: ["u"],
    includes: ["w"],
    modulePath: "static/app/modules/consumer.js",
    outRoot: fixture.outRoot,
  });
  assertGeneratedModuleAfterEntryScript({
    expectedStdout: "d\n",
    outRoot: fixture.outRoot,
    source: `const { w } = await import("./static/app/modules/consumer.js");
console.log(w({ a: null, b: "d" }));
`,
  });
  assertEntryOutput(fixture, "c\n");
});

// --- Naturalization heuristics --------------------------------------------

test("renames destructured object params to readable shorthand", async () => {
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-readable-object-params-",
    source: `function a({ value: n, count: e }) { return n + e; }
console.log(a({ value: 1, count: 2 }));
export { a };
`,
    operations: [logicalModule("x", [{ name: "pair", binding: "a", kind: "FunctionDeclaration" }])],
  });
  assertModuleSource({
    matches: [/function pair\(\{\s*value,\s*count\s*\}\)/, /return value \+ count;/],
    doesNotMatch: [/value: n/, /count: e/],
    modulePath: "static/app/modules/x.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "3\n");
});

test("renames constructor params from this-property assignments", async () => {
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-readable-constructor-params-",
    source: `class A {
  constructor(n, e) { this.value = n; this.count = e; }
}
console.log(new A(1, 2).value, new A(1, 2).count);
export { A };
`,
    operations: [logicalModule("x", [{ name: "Pair", binding: "A", kind: "ClassDeclaration" }])],
  });
  assertModuleSource({
    matches: [/constructor\(value, count\)/, /this\.value = value;/, /this\.count = count;/],
    doesNotMatch: [/constructor\(n, e\)/],
    modulePath: "static/app/modules/x.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "1 2\n");
});

test("renames return-object aliases to readable shorthand locals", async () => {
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-readable-return-object-",
    source: `function a(o) {
  const n = o.value;
  const e = o.count;
  return { value: n, count: e };
}
console.log(JSON.stringify(a({ value: 1, count: 2 })));
export { a };
`,
    operations: [logicalModule("x", [{ name: "pair", binding: "a", kind: "FunctionDeclaration" }])],
  });
  assertModuleSource({
    matches: [/\bconst value = o\.value;/, /\bconst count = o\.count;/, /return \{\s*value,\s*count\s*\};/],
    doesNotMatch: [/value: n/, /count: e/],
    modulePath: "static/app/modules/x.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, '{"value":1,"count":2}\n');
});

// --- URL rebasing ---------------------------------------------------------

test("rebases worker constructor URL to runtime-relative module URL", async () => {
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-worker-url-rebase-",
    source: `function a() { return new Worker("./b.js"); }
console.log(typeof a);
export { a };
`,
    operations: [logicalModule("x", [{ name: "a", kind: "FunctionDeclaration" }])],
  });
  assertModuleSource({
    matches: [/new Worker\(new URL\("\.\.\/b\.js", import\.meta\.url\)\)/],
    modulePath: "static/app/modules/x.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "function\n");
});

test("rebases dynamic import specifiers to runtime-relative paths", async () => {
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-dynamic-import-rebase-",
    source: `async function a() { const m = await import("./b.js"); return m.x; }
console.log(typeof a);
export { a };
`,
    operations: [logicalModule("x", [{ name: "a", kind: "FunctionDeclaration" }])],
  });
  assertModuleSource({
    matches: [/await import\("\.\.\/b\.js"\)/],
    modulePath: "static/app/modules/x.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "function\n");
});

// --- Rejections -----------------------------------------------------------

test("rejects extraction with a propagated final-name collision", async () => {
  // Two members both renamed to "a" — one from the variable `a`, one from
  // function `b`. The extractor should refuse before emitting.
  await expectLogicalModulesE2eRejection({
    prefix: "debundle-e2e-reject-collision-",
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
