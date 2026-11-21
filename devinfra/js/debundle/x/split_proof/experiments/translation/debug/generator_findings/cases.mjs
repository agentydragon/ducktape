// Programs that the footprint oracle (`footprint_oracle.mjs`) or the generators found wrong, reduced to the format of
// `experiments/effects/cases.mjs`: one unit per line, and a reordered program that Node prints differently and `translate`
// must refuse. Each of them is accepted wrongly by the analysis as it is today. Run, from `experiments/effects`:
//   CASES=../translation/debug/generator_findings/cases.mjs node run.mjs
// A `FAIL … WRONG ACCEPT` is a finding that stands. A line that passes means the author fixed it: move the case to
// `experiments/effects/cases_general.mjs`. See README.md for the table of causes.
//
// Premise P3 is on: it is the setting of the fuzzers and of the large bundle. F6 was found by `gen/`.
export const premises = { noWorldHooks: true };

const finding = (name, source, order) => ({
  name,
  source: source.trim() + "\n",
  reorders: [{ order, behavior: "differs" }],
});

export const cases = [
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
