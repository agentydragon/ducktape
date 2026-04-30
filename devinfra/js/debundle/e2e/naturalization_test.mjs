// Naturalization heuristics: the lowered modules should rename scrambled
// destructured/aliased identifiers to the readable property names that
// surround them. Black-box: runs run_transform with a JSONC spec and
// regex-matches the emitted module file.
import test from "node:test";
import { assertEntryOutput, assertModuleSource, logicalModule, runLogicalModulesE2eFixture } from "./support.mjs";

test("renames destructured object params to readable shorthand", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
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

test("renames constructor params from this-property assignments", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
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

test("renames return-object aliases to readable shorthand locals", async (t) => {
  const fixture = await runLogicalModulesE2eFixture(t, {
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
