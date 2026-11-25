import assert from "node:assert/strict";
import {
  assertCertifiedResult,
  assertRejectedOrOrderKeptSlice,
  lower,
  sequentialSplit,
  workerSplit,
} from "./harness.mjs";

{
  const test = workerSplit(
    "array-rest-pattern",
    `function pick(head, ...[first, , third]) {
       var first;
       first = first + 1;
       return [head, first, third, arguments.length];
     }
     const arrow = (...[x, y]) => x + y;
     export const result = [pick("head", 2, 3, 4), pick.length, arrow(4, 3), arrow.length];`,
    ["#1", "#2"]
  );
  assertCertifiedResult(test, [["head", 3, 4, 4], 1, 7, 0]);
  assert.equal(
    lower(test.files.sourceFile, test.files.specFile, test.out, ["--check-output"]).verdict.status,
    "certified"
  );
}

{
  const test = sequentialSplit(
    "ordinary-rest",
    `function collect(first, ...items) {
       var items;
       const capture = () => items;
       let shadow;
       { let items = "shadow"; shadow = items; }
       return [first, items, capture() === items, shadow];
     }
     const holder = { value: "lexical", read(head, ...values) {
       const collect = (...items) => [this.value, head, values, items, collect.length];
       return collect(8, 9);
     } };
     const arrow = (first, ...rest) => first + rest.length;
     const run = () => {
       const a = collect("empty");
       const b = collect("empty");
       let refused = false;
       try { new arrow(7, 4, 3); } catch { refused = true; }
       const c = collect("prefix", 4, 3);
       const method = holder.read(1, 2);
       if (a[1] === b[1] || a[1].length !== 0 || c[1][0] !== 4 || c[1][1] !== 3 || !c[2] ||
           c[3] !== "shadow" || collect.length !== 1 || method[0] !== "lexical" ||
           method[2][0] !== 2 || method[3][1] !== 9 || method[4] !== 0 || arrow.length !== 1 ||
           arrow(7, 4, 3) !== 9 || !refused) throw "incorrect rest call";
       return [a[1] !== b[1], a, c, collect.length, method, arrow.length, arrow(7, 4, 3), refused];
     };
     export const result = run();`,
    { "worker.mjs": ["#0", "#1", "#2", "#3"], "entry.mjs": ["#4"] }
  );
  assertCertifiedResult(test, [
    true,
    ["empty", [], true, "shadow"],
    ["prefix", [4, 3], true, "shadow"],
    1,
    ["lexical", 1, [2], [8, 9], 0],
    1,
    9,
    true,
  ]);
  assert.equal(
    lower(test.files.sourceFile, test.files.specFile, test.out, ["--check-output"]).verdict.status,
    "certified"
  );
}

{
  const test = sequentialSplit(
    "constructor-rest-and-super-forwarding",
    `class Base {
       constructor(first, ...tail) { this.first = first; this.tail = tail; }
     }
     class Child extends Base {
       constructor(...values) {
         const count = values.length;
         (super(...values), this.label = "child", this.count = count);
       }
     }
     function Record(first, ...rest) { this.first = first; this.rest = rest; }
     const run = () => {
       const a = new Child(1, 2, 3);
       const b = new Child(1);
       const record = new Record(4, 5);
       let refused = false;
       try { Child(1); } catch { refused = true; }
       if (a.first !== 1 || a.tail[0] !== 2 || a.tail[1] !== 3 || a.label !== "child" ||
           a.count !== 3 || b.tail.length !== 0 || a.tail === b.tail || record.first !== 4 ||
           record.rest[0] !== 5 || Base.length !== 1 || Child.length !== 0 || Record.length !== 1 ||
           !refused) throw "incorrect rest construction";
       return [a.first, a.tail, a.label, a.count, b.tail, a.tail !== b.tail, record.first, record.rest,
         Base.length, Child.length, Record.length, refused];
     };
     export const result = run();`,
    { "worker.mjs": ["#0", "#1", "#2", "#3"], "entry.mjs": ["#4"] }
  );
  assertCertifiedResult(test, [1, [2, 3], "child", 3, [], true, 4, [5], 1, 0, 1, true]);
}

assertRejectedOrOrderKeptSlice(
  workerSplit(
    "reached-rest-with-default",
    "const fn = (first = 1, ...rest) => rest.length; export const result = fn();"
  ),
  /rest requires a terminal binding and fixed identifier parameters/
);
