// Programs the analysis accepts in an order Node runs differently: limits of its general rules (see README.md). Run with
//   CASES=debug/general_known_gaps/cases.mjs node run.mjs
// from `experiments/effects`: each `FAIL … WRONG ACCEPT` is a known limit. A line that passes means a limit is gone:
// move the case to `cases.mjs`.
export const premises = {};
const gap = (name, source, order) => ({ name, source, reorders: [{ order, behavior: "differs" }] });
export const cases = [
  gap(
    "a method builds its result through a species constructor that the program assigned",
    `
const o = { n: 0 };
const arr = [1];
arr.constructor = { [Symbol.species]: function () { o.n = 1; return {}; } };
arr.map((x) => x);
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  gap(
    "Reflect.get runs a getter with another receiver",
    `
const target = { n: 0 };
const src = { get v() { this.n = 1; return 1; } };
Reflect.get(src, "v", target);
console.log(target.n);
`,
    [0, 1, 3, 2]
  ),
  gap(
    "Reflect.set stores into another receiver",
    `
const o = {};
const target = { n: 0 };
Reflect.set(o, "n", 1, target);
console.log(target.n);
`,
    [0, 1, 3, 2]
  ),
  gap(
    "String.raw reads a getter two levels into its argument",
    `
const o = { n: 0 };
String.raw({ raw: { get length() { o.n = 1; return 0; } } });
console.log(o.n);
`,
    [0, 2, 1]
  ),
  gap(
    "a static of Array called on a subclass constructs the subclass",
    `
const o = { n: 0 };
class MyArr extends Array { constructor() { super(); o.n++; } }
MyArr.from([1]);
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  gap(
    "console.log of an object that holds a util.inspect.custom method (Node only)",
    `
const o = { n: 0 };
const obj = { [Symbol.for("nodejs.util.inspect.custom")]() { o.n = 1; return "x"; } };
console.log({ nested: obj });
const w = o.n;
console.log(w);
`,
    [0, 1, 3, 2, 4]
  ),
];
