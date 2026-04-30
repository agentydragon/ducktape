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
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-split-declarator-order-",
    source: `const a = 1, b = 2, c = 3, d = a + b, e = d + c, f = e;
const z = "z";
console.log(f);
export { f, z };
`,
    operations: [logicalModule("x", [{ name: "f" }])],
  });
  assertModuleExports({ includes: ["f"], modulePath: "static/app/modules/x.js", outRoot: fixture.outRoot });
  assertModuleExports({
    excludes: ["f"],
    modulePath: "static/app/modules/residual/unhandled.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "6\n");
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
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-init-wrapper-",
    source: `globalThis.events = [];
const a = (globalThis.events.push("a"), 1);
function b() { return a; }
const c = (globalThis.events.push("c"), b());
console.log(globalThis.events.join(","), c);
export { c };
`,
    operations: [logicalModule("x", [{ name: "a" }, { name: "b", kind: "FunctionDeclaration" }])],
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
  assertEntryOutput(fixture, "a,c 1\n");
});

// --- Cross-module dependency wiring ---------------------------------------

test("closes an extracted module over its helper dependencies", async () => {
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-helper-closure-",
    source: `const q = m => { throw TypeError(m); };
const r = x => { if (typeof x !== "object") q("a"); return x.a(); };
function s() { return r({ a: () => "b" }); }
console.log(s());
export { s };
`,
    operations: [logicalModule("x", [{ name: "extracted", binding: "r" }])],
  });
  assertModuleExports({ includes: ["extracted"], modulePath: "static/app/modules/x.js", outRoot: fixture.outRoot });
  assertModuleExports({
    excludes: ["q"],
    modulePath: "static/app/modules/residual/unhandled.js",
    outRoot: fixture.outRoot,
  });
  assertGeneratedModuleAfterEntryScript({
    expectedStdout: "c\nTypeError\n",
    outRoot: fixture.outRoot,
    source: `const { extracted } = await import("./static/app/modules/x.js");
console.log(extracted({ a: () => "c" }));
try { extracted(1); } catch (e) { console.log(e.name); }
`,
  });
  assertEntryOutput(fixture, "b\n");
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

test("imports lazy destructuring dependencies through renames", async () => {
  const fixture = await runLogicalModulesE2eFixture({
    prefix: "debundle-e2e-destructuring-fragment-dependency-",
    source: `const q = (o, k) => ({ x: o.a.get(k), y: "a" }),
  r = (o, k) => ({ x: o.b.get(k), y: "b" });
const s = x => x,
  t = x => s(x),
  u = o => {
    const { a, c } = o, d = a.get("d");
    const { x, y } = d !== "c" ? q(o, "x") : c.e ? r(o, "x") : q(o, "x");
    return t(x ?? y);
  };
console.log(u({ a: new Map([["x", "p"]]), b: new Map(), c: { e: false } }));
export { u };
`,
    operations: [
      logicalModule("provider", [
        { name: "f", binding: "q" },
        { name: "g", binding: "r" },
      ]),
      logicalModule("consumer", [
        { name: "h", binding: "s" },
        { name: "i", binding: "t" },
        { name: "j", binding: "u" },
      ]),
    ],
  });
  assertModuleExports({ includes: ["f", "g"], modulePath: "static/app/modules/provider.js", outRoot: fixture.outRoot });
  assertModuleExports({
    excludes: ["f"],
    includes: ["j"],
    modulePath: "static/app/modules/consumer.js",
    outRoot: fixture.outRoot,
  });
  assertGeneratedModuleAfterEntryScript({
    expectedStdout: "q\n",
    outRoot: fixture.outRoot,
    source: `const { j } = await import("./static/app/modules/consumer.js");
console.log(j({ a: new Map([["d", "c"]]), b: new Map([["x", "q"]]), c: { e: true } }));
`,
  });
  assertEntryOutput(fixture, "p\n");
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
  await expectLogicalModulesE2eRejection({
    prefix: "debundle-e2e-reject-collision-",
    source: `const a = 1;
function b() { return a; }
function c() { return b(); }
console.log(c());
export { c };
`,
    operations: [logicalModule("x", [{ name: "a" }, { name: "a", binding: "b", kind: "FunctionDeclaration" }])],
    errorPattern:
      /propagated final name collision|conflicts with existing top-level binding|duplicate binding name|duplicate exported logical names/i,
  });
});
