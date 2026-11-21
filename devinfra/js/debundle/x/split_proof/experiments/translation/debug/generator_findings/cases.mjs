// Programs that the footprint oracle (`footprint_oracle.mjs`) or the generators found wrong, reduced to the format of
// `experiments/effects/cases.mjs`: one unit per line, and a reordered program that Node prints differently and `translate`
// must refuse. Each of them is accepted wrongly by the analysis as it is today. Run, from `experiments/effects`:
//   CASES=../translation/debug/generator_findings/cases.mjs node run.mjs
// A `FAIL … WRONG ACCEPT` is a finding that stands. A line that passes means the author fixed it: move the case to
// `experiments/effects/cases_general.mjs`. See README.md for the table of causes.
//
// Premise P3 is on: it is the setting of the fuzzers and of the large bundle. F1 and F2 are the same as existing golden
// cases (`cases_general.mjs`) with a pure reader added, or with P3 on instead of off. F6 was found by `gen/`.
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
  // F6: a Proxy is an object the world returned, and with premise P3 on a read of a property of such an object is pure; the traps are
  // the program's own code, which the read runs. `set`, `deleteProperty`, `defineProperty`, `apply` and a method call through the
  // proxy are refused. With P3 off every program here is refused.
  finding(
    "F6 get trap: a log of a field of the proxy",
    `
const hits = { n: 0 };
const t = { f: 1 };
const px = new Proxy(t, { get(target, key, receiver) { hits.n += 1; return Reflect.get(target, key, receiver); } });
console.log(px.f);
const seen = hits.n;
console.log(seen);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  finding(
    "F6 get trap: a pure read of a field of the proxy",
    `
const hits = { n: 0 };
const t = { f: 1 };
const px = new Proxy(t, { get(target, key, receiver) { hits.n += 1; return Reflect.get(target, key, receiver); } });
const x = px.f;
const seen = hits.n;
console.log(seen, x);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  finding(
    "F6 has trap: key in proxy",
    `
const hits = { n: 0 };
const t = { f: 1 };
const px = new Proxy(t, { has(target, key) { hits.n += 3; return Reflect.has(target, key); } });
const x = "f" in px;
const seen = hits.n;
console.log(seen, x);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  finding(
    "F6 ownKeys trap: Object.keys of the proxy",
    `
const hits = { n: 0 };
const t = { f: 1 };
const px = new Proxy(t, { ownKeys(target) { hits.n += 5; return Reflect.ownKeys(target); } });
const x = Object.keys(px).length;
const seen = hits.n;
console.log(seen, x);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  finding(
    "F6 getOwnPropertyDescriptor trap",
    `
const hits = { n: 0 };
const t = { f: 1 };
const px = new Proxy(t, { getOwnPropertyDescriptor(target, key) { hits.n += 6; return Reflect.getOwnPropertyDescriptor(target, key); } });
const x = Object.getOwnPropertyDescriptor(px, "f").value;
const seen = hits.n;
console.log(seen, x);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  finding(
    "F6 getPrototypeOf trap: instanceof",
    `
const hits = { n: 0 };
const t = { f: 1 };
const px = new Proxy(t, { getPrototypeOf(target) { hits.n += 8; return Reflect.getPrototypeOf(target); } });
const x = px instanceof Object;
const seen = hits.n;
console.log(seen, x);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  finding(
    "F6 get trap: a template literal of a field of the proxy",
    `
const hits = { n: 0 };
const t = { f: 1 };
const px = new Proxy(t, { get(target, key, receiver) { hits.n += 1; return Reflect.get(target, key, receiver); } });
const x = \`\${px.f}\`;
const seen = hits.n;
console.log(seen, x);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  finding(
    "F6 get trap: spread of the proxy",
    `
const hits = { n: 0 };
const t = { f: 1 };
const px = new Proxy(t, { get(target, key, receiver) { hits.n += 1; return Reflect.get(target, key, receiver); } });
const x = { ...px }.f;
const seen = hits.n;
console.log(seen, x);
`,
    [0, 1, 2, 4, 3, 5]
  ),
];
