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
} from "./linked_programs_test_support.mjs";

const entry = "entry.mjs";
function split(name, source, modules, order) {
  const dir = path.join(root, name);
  const files = prepare(dir, { source, provider: "", spec: { entry, modules, order, sequential: true } });
  const out = path.join(dir, "out");
  return { dir, files, out, ...lower(files.sourceFile, files.specFile, out) };
}

const inherited = split(
  "default-derived",
  `class Base {
    static label = "base";
    saved = "before";
    constructor(value) { this.saved = value; }
    get value() { return this.saved; }
    set value(next) { this.saved = next; }
    describe() { return this.value; }
  }
  const Child = class extends Base {};
  const run = () => {
    const obj = new Child("constructed");
    const first = obj.describe();
    obj.value = "after";
    return [Child.label, Child.name, first, obj.value, obj.constructor === Child];
  };
  export const result = run();`,
  { "base.mjs": ["#0"], "child.mjs": ["#1"], [entry]: ["#2", "#3"] },
  ["base.mjs", "child.mjs"]
);
assert.equal(inherited.verdict.status, "certified", inherited.log);
assert.equal(
  lower(inherited.files.sourceFile, inherited.files.specFile, inherited.out, ["--check-output"]).verdict.status,
  "certified"
);
for (const file of [inherited.files.sourceFile, path.join(inherited.out, entry)]) {
  assert.deepEqual(observe(file, "result"), { result: ["base", "Child", "constructed", "after", true] });
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
