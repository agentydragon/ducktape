// Naturalization heuristics: the lowered modules should rename scrambled
// destructured/aliased identifiers to the readable property names that
// surround them. Black-box: runs run_transform with a JSONC spec and
// regex-matches the emitted module file.
import test from "node:test";
import { assertEntryOutput, assertModuleSource, logicalModule, runLogicalModulesE2eFixture } from "./support.mjs";

test("renames destructured object params to readable shorthand", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
    source: `function a({ value: n }) { return n; }
console.log(a({ value: 1 }));
export { a };
`,
    operations: [logicalModule("x", [{ name: "pair", binding: "a", kind: "FunctionDeclaration" }])],
  });
  assertModuleSource({
    matches: [/function pair\(\{\s*value\s*\}\)/, /return value;/],
    doesNotMatch: [/value: n/],
    modulePath: "static/app/modules/x.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "1\n");
});

test("renames constructor params from this-property assignments", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
    source: `class A {
  constructor(n) { this.value = n; }
}
console.log(new A(1).value);
export { A };
`,
    operations: [logicalModule("x", [{ name: "Pair", binding: "A", kind: "ClassDeclaration" }])],
  });
  assertModuleSource({
    matches: [/constructor\(value\)/, /this\.value = value;/],
    doesNotMatch: [/constructor\(n\)/],
    modulePath: "static/app/modules/x.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, "1\n");
});

test("renames return-object aliases to readable shorthand locals", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
    source: `function a(o) {
  const n = o.value;
  return { value: n };
}
console.log(JSON.stringify(a({ value: 1 })));
export { a };
`,
    operations: [logicalModule("x", [{ name: "pair", binding: "a", kind: "FunctionDeclaration" }])],
  });
  assertModuleSource({
    matches: [/\bconst value = o\.value;/, /return \{\s*value\s*\};/],
    doesNotMatch: [/value: n/],
    modulePath: "static/app/modules/x.js",
    outRoot: fixture.outRoot,
  });
  assertEntryOutput(fixture, '{"value":1}\n');
});
