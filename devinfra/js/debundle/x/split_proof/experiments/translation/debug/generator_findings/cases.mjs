// Programs that the footprint oracle (`footprint_oracle.mjs`) or the generators found wrong, reduced to the format of
// `experiments/effects/cases.mjs`: one unit per line, and a reordered program that Node prints differently and `translate`
// must refuse. Each of them is accepted wrongly by the analysis as it is today. Run, from `experiments/effects`:
//   CASES=../translation/debug/generator_findings/cases.mjs node run.mjs
// A `FAIL … WRONG ACCEPT` is a finding that stands. A line that passes means the author fixed it: move the case to
// `experiments/effects/cases_general.mjs`. See README.md for the table of causes.
//
// Premise P3 is on: it is the setting of the fuzzers and of the large bundle. F1 and F2 are the same as existing golden
// cases (`cases_general.mjs`) with a pure reader added, or with P3 on instead of off. F3 was found by `gen/`.
export const premises = { noWorldHooks: true };

const finding = (name, source, order) => ({
  name,
  source: source.trim() + "\n",
  reorders: [{ order, behavior: "differs" }],
});

export const cases = [
  // F1: the result of an iterator helper of a Map or Set iterator is a primitive for the analysis, and consuming it runs the callback
  finding(
    "F1 toArray of an iterator helper runs the callback of map",
    `
const o = { n: 0 };
const mapped = new Map([["k", 1]]).values().map((x) => { o.n++; return x; });
mapped.toArray();
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  finding(
    "F1 next of an iterator helper runs the callback of map",
    `
const o = { n: 0 };
const mapped = new Map([["k", 1]]).values().map((x) => { o.n++; return x; });
mapped.next();
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  finding(
    "F1 a spread of an iterator helper runs the callback of map",
    `
const o = { n: 0 };
const mapped = new Set([1]).values().map((x) => { o.n++; return x; });
[...mapped];
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  finding(
    "F1 for-of over an iterator helper runs the callback of filter",
    `
const o = { n: 0 };
const kept = new Set([1]).values().filter((x) => { o.n++; return true; });
for (const x of kept) {}
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  // F2: a getter that is more than 10 prototypes up is not run, and the read is not an effect (`getVal`, `seen.length > 10`)
  finding(
    "F2 a getter 11 prototypes up writes o",
    `
const o = { n: 0 };
const p0 = { get g() { o.n = 1; return 1; } };
const p1 = Object.create(p0);
const p2 = Object.create(p1);
const p3 = Object.create(p2);
const p4 = Object.create(p3);
const p5 = Object.create(p4);
const p6 = Object.create(p5);
const p7 = Object.create(p6);
const p8 = Object.create(p7);
const p9 = Object.create(p8);
const p10 = Object.create(p9);
const p11 = Object.create(p10);
p11.g;
console.log(o.n);
`,
    [...Array(13).keys(), 14, 13]
  ),
  // F3: `new Array(n)` and `Array(n)` are plain objects for the analysis (`("Array", { ret := fresh, … })`), so no array method is found
  finding(
    "F3 forEach of new Array(1).fill(7) runs a callback that writes o",
    `
const o = { n: 0 };
const a = new Array(1).fill(7);
a.forEach(() => { o.n++; });
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  finding(
    "F3 Array(2).fill(o) stores o, and a write through the array writes o",
    `
const o = { n: 0 };
const a = Array(2).fill(o);
a[0].n = 1;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
];
