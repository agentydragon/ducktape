// Programs the analysis accepts in an order Node runs differently: the limits of premise P1 (see README.md). Run with
//   CASES=debug/p1_known_gaps/cases.mjs node run.mjs
// from `experiments/effects`: each `FAIL … WRONG ACCEPT` is a known limit. A line that passes means a limit is gone:
// move the case to `cases.mjs`.
export const premises = {};
const gap = (name, source, order) => ({ name, source, reorders: [{ order, behavior: "differs" }] });
export const cases = [
  gap(
    "a standard function called through a computed name",
    `
const k = "defineProperty";
Object[k](Object.prototype, "tag", { value: "patched", configurable: true });
const seen = ({}).tag;
console.log(seen);
`,
    [0, 2, 1, 3]
  ),
  gap(
    "a standard function forwarded through a spread wrapper",
    `
const apply = (f, ...a) => f(...a);
apply(Object.defineProperty, Object.prototype, "tag", { value: "patched", configurable: true });
const seen = ({}).tag;
console.log(seen);
`,
    [0, 2, 1, 3]
  ),
  gap(
    "a patch through an alias in code the analysis does not run",
    `
function* g() { const O = Object; O.prototype.tag = "patched"; yield 1; }
g().next();
const seen = ({}).tag;
console.log(seen);
`,
    [0, 2, 1, 3]
  ),
  gap(
    "a global defined with defineProperty on the global object",
    `
Object.defineProperty(globalThis, "Array", { value: class { static isArray() { return "patched"; } }, configurable: true, writable: true });
const r = Array.isArray([]);
console.log(r);
`,
    [1, 0, 2]
  ),
  gap(
    "the legacy static state of RegExp that exec writes",
    `
/(a)(b)/.exec("ab");
const first = RegExp.$1;
console.log(first);
`,
    [1, 0, 2]
  ),
];
