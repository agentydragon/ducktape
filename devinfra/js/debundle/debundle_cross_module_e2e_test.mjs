// Cross-module dependency wiring tests. Black-box: runs run_transform with a
// JSONC spec and asserts on emitted modules + runtime equivalence.
import test from "node:test";
import {
  assertEntryOutput,
  assertGeneratedModuleAfterEntryScript,
  assertModuleExports,
  assertModuleSource,
  logicalModule,
  runLogicalModulesE2eFixture,
} from "./pipeline_e2e_support.mjs";

test("closes an extracted module over its helper dependencies", async (t) => {
  // Selecting only `b`. Its helper `a` must be pulled into the module file
  // (as an internal binding, not exported) and removed from residual.
  const fixture = await runLogicalModulesE2eFixture(t, {
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

test("duplicates a shared bootstrap dependency into each named module", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
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

test("imports renamed dependencies across split declarators", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
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
