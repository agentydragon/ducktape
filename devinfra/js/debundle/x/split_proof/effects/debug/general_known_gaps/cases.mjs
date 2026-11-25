// Programs the analysis accepts in an order Node runs differently: limits of its general rules (see README.md). Run with
//   CASES=debug/general_known_gaps/cases.mjs node run.mjs
// from `effects`: each `FAIL … WRONG ACCEPT` is a known limit. A line that passes means a limit is gone:
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
