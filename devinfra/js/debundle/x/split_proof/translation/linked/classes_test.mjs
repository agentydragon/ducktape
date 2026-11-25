import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  assertCertifiedResult,
  assertRejectedOrOrderKeptSlice,
  caseShard,
  exportNamed,
  forcePreview,
  id,
  literal,
  lower,
  moduleEvaluationOrder,
  observe,
  program,
  sequentialSplit,
  variable,
} from "./harness.mjs";

const entry = "entry.mjs";
const runCase = caseShard();
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
  if (!runCase(name)) continue;
  const test = sequentialSplit(name, source, { "class.mjs": ["#0"], [entry]: ["#1", "#2"] }, ["class.mjs"]);
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

// These guards run during module initialization, so certification requires
// the Lean evaluator to observe the same constructor behavior as Node.
for (const [name, source] of [
  [
    "super-replacement-receiver",
    `const label = "saved";
    class Base { constructor(a, b) { return { first: a, second: b }; } }
    class Child extends Base { constructor(x, y) { (super(y, x), this.label = label); } }
    const value = new Child("one", "two");
    const checked = (() => {
      if (value.first !== "two" || value.second !== "one" || value.label !== "saved" || Child.length !== 2) throw 1;
      return true;
    })();
    export const result = checked;`,
  ],
  [
    "super-preserves-new-target",
    `class Base { constructor(arg) { this.arg = arg; } }
    class Middle extends Base { constructor(unused) { super(); } }
    class Leaf extends Middle { read() { return 3; } }
    const value = new Leaf("unused");
    const checked = (() => {
      if (value.arg !== undefined || value.read() !== 3) throw 1;
      return true;
    })();
    export const result = checked;`,
  ],
  [
    "super-return-override",
    `class Base {}
    class Child extends Base { constructor(x) { super(); return x; } }
    const other = {};
    const value = new Child(other);
    const checked = (() => {
      if (value !== other) throw 1;
      return true;
    })();
    export const result = checked;`,
  ],
]) {
  if (!runCase(name)) continue;
  const test = sequentialSplit(name, source, { "first.mjs": ["#0"], [entry]: ["#1", "#2", "#3", "#4", "#5"] }, [
    "first.mjs",
  ]);
  assertCertifiedResult(test, true);
}

for (const [name, base, body] of [
  ["primitive-derived-return", "class Base {}", "super(); return 1;"],
  ["throwing-super", "class Base { constructor() { null.missing; } }", "super(); return {};"],
]) {
  if (!runCase(name)) continue;
  const source = `${base}
    class Child extends Base { constructor() { ${body} } }
    export const result = new Child();`;
  const test = sequentialSplit(name, source, { "base.mjs": ["#0"], [entry]: ["#1", "#2"] }, ["base.mjs"]);
  assertRejectedOrOrderKeptSlice(test, /original threw during linked initialization/);
  assert.deepEqual(observe(test.files.sourceFile, "result"), { error: "TypeError" });
}

// A field initializer executes at class creation for static fields and at new
// for instance fields. Force the defining module ahead of its sibling in both variants.
for (const isStatic of [false, true]) {
  for (const dependent of [false, true]) {
    if (!runCase(`field-${isStatic}-${dependent}`)) continue;
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
    const test = sequentialSplit(
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
    "pre-super-receiver",
    "const B = class extends null { constructor() { this.x = 1; super(); } }; export const result = 1;",
    /before super|leading super call/i,
  ],
  [
    "repeated-super",
    "const B = class extends null { constructor() { super(); super(); } }; export const result = 1;",
    /super|derived constructor/i,
  ],
]) {
  if (!runCase(name)) continue;
  const test = sequentialSplit(name, source, { "class.mjs": ["#0"], [entry]: ["#1"] }, ["class.mjs"]);
  assert.equal(test.verdict.status, "rejected", test.log);
  assert.match(test.verdict.reason, expectedReason);
  assert(!existsSync(test.out));
}

if (runCase("computed-literal-method")) {
  const literalMethod = sequentialSplit(
    "computed-literal-method",
    "const B = class { ['x']() { return this; } }; export const result = B.prototype.x.name === 'x' && B.prototype.x() === B.prototype;",
    { "class.mjs": ["#0"], [entry]: ["#1"] },
    ["class.mjs"]
  );
  assertCertifiedResult(literalMethod, true);
}

if (runCase("named-class-expression-inner-scope")) {
  const namedInnerScope = sequentialSplit(
    "named-class-expression-inner-scope",
    `let Named = "outer";
  const C = class Named {
    static self = Named;
    static rejectsWrite() {
      try { Named = 1; return false; }
      catch (error) { return true; }
    }
    self() { return Named; }
  };
  export const result = C.self === C && C.rejectsWrite() && new C().self() === C &&
    Named === "outer" && C.name === "Named";`,
    { [entry]: ["#0", "#1", "#2"] },
    []
  );
  assertCertifiedResult(namedInnerScope, true);
}
