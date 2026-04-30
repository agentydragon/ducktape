// URL-rebasing tests: when a function containing a relative URL literal moves
// from the entry into a nested module, the URL string must be rewritten so it
// still resolves to the same target. Black-box: runs run_transform with a
// JSONC spec and regex-matches the emitted module file.
import test from "node:test";
import {
  assertEntryOutput,
  assertModuleSource,
  logicalModule,
  runLogicalModulesE2eFixture,
} from "./pipeline_e2e_support.mjs";

test("rebases worker constructor URL to runtime-relative module URL", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
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

test("rebases dynamic import specifiers to runtime-relative paths", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
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
