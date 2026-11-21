// Programs that the footprint oracle (`footprint_oracle.mjs`) or the generators found wrong, reduced to the format of
// `experiments/effects/cases.mjs`: one unit per line, and a reordered program that Node prints differently and `translate`
// must refuse. Each of them is accepted wrongly by the analysis as it is today. Run, from `experiments/effects`:
//   CASES=../translation/debug/generator_findings/cases.mjs node run.mjs
// A `FAIL … WRONG ACCEPT` is a finding that stands. A line that passes means the author fixed it: move the case to
// `experiments/effects/cases_general.mjs`. See README.md for the table of causes.
//
// Premise P3 is on: it is the setting of the fuzzers and of the large bundle, and what F7 is about.
export const premises = { noWorldHooks: true };

const finding = (name, source, order) => ({
  name,
  source: source.trim() + "\n",
  reorders: [{ order, behavior: "differs" }],
});

export const cases = [
  // F7: code of the program that is not run here (a callback the world runs, a generator body) or a standard function without a
  // row defines a getter or a conversion hook on an object of the program. With premise P3 a read of that object is plain
  finding(
    "F7 a callback the world runs defines a getter on an object of the program",
    `
const o = { n: 0 };
const obj = {};
const install = () => { Object.defineProperty(obj, "g", { get() { o.n++; return 1; } }); };
[1].values().forEach(install);
obj.g;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 2, 3, 5, 4, 6]
  ),
  finding(
    "F7 a callback the world runs stores a toString into an object of the program",
    `
const o = { n: 0 };
const obj = {};
const install = () => { obj.toString = () => { o.n++; return "s"; }; };
[1].values().forEach(install);
\`\${obj}\`;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 2, 3, 5, 4, 6]
  ),
  finding(
    "F7 a callback the world runs gives an object of the program a prototype with a getter",
    `
const o = { n: 0 };
const obj = {};
const install = () => { Object.setPrototypeOf(obj, { get g() { o.n++; return 1; } }); };
[1].values().forEach(install);
obj.g;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 2, 3, 5, 4, 6]
  ),
  finding(
    "F7 a generator body defines a getter on an object of the program",
    `
const o = { n: 0 };
const obj = {};
function* install() { Object.defineProperty(obj, "g", { get() { o.n++; return 1; } }); }
install().next();
obj.g;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 2, 3, 5, 4, 6]
  ),
  finding(
    "F7 Reflect.apply of Object.defineProperty defines a getter",
    `
const o = { n: 0 };
const obj = {};
Reflect.apply(Object.defineProperty, null, [obj, "g", { get() { o.n++; return 1; } }]);
obj.g;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 2, 4, 3, 5]
  ),
];
