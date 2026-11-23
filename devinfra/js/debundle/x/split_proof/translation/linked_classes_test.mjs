import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  exportNamed,
  forcePreview,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  prepare,
  program,
  root,
  variable,
} from "./testing/linked_programs.mjs";

const entry = "entry.mjs";
function split(name, source, modules, order) {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider: "", spec: { entry, modules, order, sequential: true } });
  const out = path.join(dir, "out");
  return { dir, files, out, ...lower(files.sourceFile, files.specFile, out) };
}

for (const [name, source, expected] of [
  [
    "default-derived",
    `const Child = class extends null {};
    const name = Child.name;
    export const result = name;`,
    "Child",
  ],
  [
    "eager-heritage",
    `class Base { static label = "base"; }
    const Child = class extends Base {};
    export const result = Child.label;`,
    "base",
  ],
  [
    "deferred-heritage",
    `class Base { static label = "base"; }
    const read = () => (class extends Base {}).label;
    export { read as result };`,
    "base",
  ],
  [
    "constructor",
    `const Box = class { constructor(value) { this.saved = value; } };
    const value = new Box("constructed");
    export const result = value.saved;`,
    "constructed",
  ],
  [
    "accessors",
    `class Box {
      static get value() { return this.saved; }
      static set value(v) { this.saved = v; }
      static read() { return this.value; }
    }
    Box.value = "after";
    export const result = Box.read();`,
    "after",
  ],
]) {
  const test = split(name, source, { "class.mjs": ["#0"], [entry]: ["#1", "#2"] }, ["class.mjs"]);
  assert.equal(test.verdict.status, "certified", name + ": " + test.log);
  if (name === "eager-heritage") {
    assert.equal(
      lower(test.files.sourceFile, test.files.specFile, test.out, ["--check-output"]).verdict.status,
      "certified"
    );
  }
  for (const file of [test.files.sourceFile, path.join(test.out, entry)]) {
    assert.deepEqual(observe(file, "result"), { result: expected });
  }
}

// A field initializer executes at class creation for static fields and at new
// for instance fields. Force the defining module ahead of its sibling in both variants.
for (const isStatic of [false, true]) {
  for (const dependent of [false, true]) {
    const field = {
      type: "PropertyDefinition",
      static: isStatic,
      computed: false,
      key: id("value"),
      value: dependent ? id("seed") : literal("fixed"),
    };
    const source = program([
      variable("const", "seed", literal("fixed")),
      variable("const", "Box", {
        type: "ClassExpression",
        id: null,
        superClass: null,
        body: { type: "ClassBody", body: [field] },
      }),
      variable("const", "result", {
        type: "MemberExpression",
        computed: false,
        optional: false,
        object: isStatic ? id("Box") : { type: "NewExpression", callee: id("Box"), arguments: [] },
        property: id("value"),
      }),
      exportNamed([["result", "result"]]),
    ]);
    const order = ["box.mjs", "seed.mjs"];
    const test = split(
      `field-${isStatic}-${dependent}`,
      source,
      { "seed.mjs": ["#0"], "box.mjs": ["#1"], [entry]: ["#2", "#3"] },
      order
    );
    assert.deepEqual(observe(test.files.sourceFile, "result"), { result: "fixed" });
    let candidate = test.out;
    if (isStatic && dependent) {
      assert.equal(test.verdict.status, "rejected", test.log);
      assert(!existsSync(candidate));
      candidate = path.join(test.dir, "preview");
      forcePreview(test.files, candidate);
    } else {
      assert.equal(test.verdict.status, "certified", test.log);
    }
    assert.deepEqual(moduleEvaluationOrder(candidate, entry), [...order, entry]);
    assert.deepEqual(
      observe(path.join(candidate, entry), "result"),
      isStatic && dependent ? { error: "ReferenceError" } : { result: "fixed" }
    );
  }
}

for (const [name, source, expectedReason] of [
  [
    "explicit-super",
    "const B = class extends null { constructor() { super(); } }; export const result = 1;",
    /explicit derived constructor/,
  ],
  ["derived-field", "const B = class extends null { value = true; }; export const result = 1;", /derived class field/],
  ["computed", "const B = class { ['x']() {} }; export const result = 1;", /computed class key/],
  ["static-block", "const B = class { static {} }; export const result = 1;", /StaticBlock/],
]) {
  const test = split(name, source, { "class.mjs": ["#0"], [entry]: ["#1"] }, ["class.mjs"]);
  assert.equal(test.verdict.status, "rejected", test.log);
  assert.match(test.verdict.reason, expectedReason);
  assert(!existsSync(test.out));
}
