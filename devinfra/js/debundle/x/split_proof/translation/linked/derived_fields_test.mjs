import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { assertCertifiedResult, lower, sequentialSplit } from "./harness.mjs";

const implicit = sequentialSplit(
  "default-derived-fields",
  `class Base {
    constructor(id) { this.id = id; }
    set items(value) { throw "inherited setter"; }
  }
  class Child extends Base {
    items = [];
    alias = this.items;
    idAtSuper = this.id;
  }
  export const result = (() => {
    const a = new Child(3), b = new Child(4);
    if (a.items === b.items || a.items !== a.alias || b.items !== b.alias) throw "instance fields";
    if (a.idAtSuper !== 3 || b.idAtSuper !== 4) throw "super arguments";
    return a.items.length;
  })();`
);
assertCertifiedResult(implicit, 0);

const { files, out } = implicit;
assert.deepEqual(lower(files.sourceFile, files.specFile, out, ["--check-output"]).verdict, implicit.verdict);
const entry = path.join(out, "entry.mjs");
const original = readFileSync(entry, "utf8");
const changed = original.replace(/items\s*=\s*\[\s*\]/, "items = [1]");
assert.notEqual(changed, original);
writeFileSync(entry, changed);
assert.match(lower(files.sourceFile, files.specFile, out, ["--check-output"]).verdict.reason, /entry\.mjs differs/);

const explicit = sequentialSplit(
  "explicit-derived-fields",
  `const label = "outer";
  class Base { constructor() { return { phase: "base" }; } }
  class Child extends Base {
    seen = this.phase;
    origin = label;
    constructor(label) {
      super();
      if (this.seen !== "base" || this.origin !== "outer") throw "field initialization";
      this.phase = label;
    }
  }
  export const result = (new Child("body")).phase;`,
  { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3"] },
  ["base.mjs"]
);
assertCertifiedResult(explicit, "body");

const abrupt = sequentialSplit(
  "abrupt-derived-fields",
  `const trace = { count: 0 };
  class Base { constructor(fail) { if (fail) throw "parent"; } }
  class Child extends Base {
    before = ++trace.count;
    failure = (() => { throw "field"; })();
    after = ++trace.count;
    constructor(fail) { super(fail); ++trace.count; }
  }
  export const result = (() => {
    try { new Child(true); } catch (error) { if (error !== "parent") throw error; }
    if (trace.count !== 0) throw "after failed parent";
    try { new Child(false); } catch (error) { if (error !== "field") throw error; }
    return trace.count;
  })();`,
  { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3"] },
  ["base.mjs"]
);
assertCertifiedResult(abrupt, 1);

const forwarded = sequentialSplit(
  "derived-fresh-arguments",
  `const identity = {};
  class Base {
    constructor(first, second, third) {
      this.values = [first === identity, second === undefined ? "empty" : second, third === undefined];
    }
  }
  class Child extends Base {
    constructor() { (super(...arguments), this.finished = true); }
  }
  class Further extends Child {}
  export const result = (() => {
    const empty = new Further(), full = new Further(identity, 8);
    if (!empty.finished || !full.finished || Child.length !== 0) throw "constructor";
    return [empty.values, full.values];
  })();`,
  { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3", "#4"] },
  ["base.mjs"]
);
assertCertifiedResult(forwarded, [
  [false, "empty", true],
  [true, 8, true],
]);
