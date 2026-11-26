import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import path from "node:path";
import {
  assertCertifiedResult,
  assertRejectedOrOrderKeptSlice,
  caseShard,
  lower,
  observe,
  prepare,
  root,
  sequentialSplit,
} from "./harness.mjs";

const runCase = caseShard();

if (runCase("selected-and-copied")) {
  const files = prepare(path.join(root, "pre-super-object-rest"), {
    provider: "",
    source: `const trace = { value: "" };
   class Base {
     constructor(id, options) { trace.value += "b"; this.id = id; this.options = options; }
   }
   class Child extends Base {
     field = trace.value += "f";
     constructor(input) {
       const { id: first, id: second, inherited, ...options } = input.record;
       super(first + second + inherited, { ...options });
       this.options.extra = 9;
     }
   }
   export const result = (() => {
     const record = { __proto__: { inherited: 1, ancestor: 8 },
       get id() { trace.value += "i"; return 3; },
       get extra() { trace.value += "e"; return 4; },
       [Symbol.iterator]: 7,
       ["__proto__"]: 5
     };
     const value = new Child({ get record() { trace.value += "r"; return record; } });
     if (trace.value !== "riiebf" || value.id !== 7) throw 1;
     const options = value.options;
     if (options.id !== undefined || options.inherited !== undefined || options.ancestor !== undefined) throw 2;
     if (options.extra !== 9 || options[Symbol.iterator] !== 7 || options.__proto__ !== 5) throw 3;
     try { new Child({ record: { get extra() { trace.value += "!"; throw 8; } } }); }
     catch (e) { if (e !== 8) throw e; }
     if (trace.value !== "riiebf!") throw 4;
     return trace.value;
   })();`,
    spec: {
      entry: "entry.mjs",
      modules: { "base.mjs": ["#0", "#1"], "entry.mjs": ["#2", "#3"] },
      order: ["base.mjs"],
      sequential: true,
    },
  });
  const profile = path.join(root, "browser.json");
  writeFileSync(profile, JSON.stringify({ profile: "browser-page" }));
  const options = ["--host-contract", profile];
  const out = path.join(root, "rest-output");
  const run = lower(files.sourceFile, files.specFile, out, options);
  assertCertifiedResult({ ...run, files, out }, "riiebf!");
  assert.deepEqual(lower(files.sourceFile, files.specFile, out, [...options, "--check-output"]).verdict, run.verdict);
}

if (runCase("own-enumerable")) {
  const enumerable = sequentialSplit(
    "pre-super-object-enumerable",
    `class Base { constructor(rest) { this.rest = rest; } }
     class Child extends Base {
       constructor(source) { var {0: first, ...rest} = source; super(rest); this.first = first; }
     }
     export const result = (() => {
       const chars = new Child("ab"), array = new Child([2, 3]);
       if (chars.first !== "a" || chars.rest[1] !== "b" || 0 in chars.rest) throw 1;
       if (array.first !== 2 || array.rest[1] !== 3 || "length" in array.rest) throw 2;
       return true;
     })();`,
    { "base.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
    ["base.mjs"]
  );
  assertCertifiedResult(enumerable, true);
}

for (const [name, body, reason] of [
  ["receiver", "const { value = this, ...rest } = {}; super(rest);", /ThisExpression before super/],
  ["tdz", "const { value = rest, ...rest } = {}; super(value);", /original threw during linked initialization/],
  ["null", "const { ...rest } = null; super(rest);", /original threw during linked initialization/],
]) {
  if (!runCase(name)) continue;
  const refused = sequentialSplit(
    `pre-super-object-${name}`,
    `class Base {}
     class Child extends Base { constructor() { ${body} } }
     export const result = new Child();`,
    { "base.mjs": ["#0"], "entry.mjs": ["#1", "#2"] },
    ["base.mjs"]
  );
  assertRejectedOrOrderKeptSlice(refused, reason);
  assert.deepEqual(observe(refused.files.sourceFile, "result"), {
    error: name === "null" ? "TypeError" : "ReferenceError",
  });
}
