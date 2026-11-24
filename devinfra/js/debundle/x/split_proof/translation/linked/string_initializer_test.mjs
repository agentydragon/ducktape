import assert from "node:assert/strict";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import { observe, root, structuralFixture } from "./harness.mjs";

const page = path.join(root, "page.json");
writeFileSync(page, JSON.stringify({ profile: "browser-page" }));
const closed = structuralFixture(
  "closed-template",
  'var marker = 0; var pattern = `^[^${"a" + "z"}]*[${"b"}]`; export { pattern };',
  ["#1"],
  ["#0", "#2"]
);
const certified = closed.run("linked-structural-pivot");
assert.equal(certified.status, "certified");
assert.deepEqual(closed.run("linked-structural-pivot", "--check-output"), certified);
for (const file of [closed.sourceFile, path.join(closed.out, "entry.mjs")]) {
  assert.deepEqual(observe(file, "pattern"), { pattern: "^[^az]*[b]" });
}

// A string declaration somewhere in the program is not a proof of what this
// cell contains at the read. Moving just the read observes undefined.
for (const [name, before, expression, expected] of [
  ["prefix", 'export var parts = "a"', "`[${parts}]`", { result: "[a]" }],
  ["getter", 'export var parts = { get text() { return "b"; } }', "`[${parts.text}]`", { result: "[b]" }],
  [
    "coercion",
    "export var parts = { toString() { throw new RangeError(); } }",
    "`[${parts}]`",
    { error: "RangeError" },
  ],
  ["ordinary-construction", "var marker = 0", 'new RegExp("a")', { result: {} }],
  ["invalid-pattern", "var marker = 0", 'new RegExp("[")', { error: "SyntaxError" }],
  [
    "patched-constructor",
    "globalThis.RegExp = class { constructor() { throw new RangeError(); } }",
    'new RegExp("a")',
    { error: "RangeError" },
  ],
  [
    "shadow",
    "export var RegExp = class { constructor() { throw new TypeError(); } }",
    'new RegExp("a")',
    { error: "TypeError" },
  ],
]) {
  const test = structuralFixture(
    name,
    `${before}; var result = ${expression}; export { result };`,
    ["#1"],
    ["#0", "#2"]
  );
  const refused = test.run("linked-structural", "--host-contract", page);
  assert.equal(refused.status, "rejected");
  assert.match(refused.reason, /unit 1 is not a local allocating declaration/);
  assert(!existsSync(test.out));
  assert.deepEqual(observe(test.sourceFile, "result"), expected);
}

// Reusing a compiled RegExp object (or copying the constructor input into
// .source) would fail these ordinary observations, even with a stable callee.
const fresh = structuralFixture(
  "regexp-constructor-results",
  'var marker = 0; var first = new RegExp("a/b"); var second = new RegExp("a/b"); const inspect = () => { first.lastIndex = 3; return [first === second, first.source, second.lastIndex, new RegExp("").source]; }; export { inspect };',
  ["#1", "#2"],
  ["#0", "#3", "#4"]
);
assert.deepEqual(observe(fresh.sourceFile, "inspect"), { inspect: [false, "a\\/b", 0, "(?:)"] });
assert.equal(fresh.run("linked-structural", "--host-contract", page).status, "rejected");
assert(!existsSync(fresh.out));
