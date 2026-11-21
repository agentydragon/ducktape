// The footprint oracle: what its snapshot sees and does not see, what it refuses as input, how it compares with footprints,
// and that it fails on a footprint that is wrong. None of it depends on the analysis being right or wrong today.
import assert from "node:assert/strict";
import { checkProgram, compare, instrument, observe } from "./footprint_oracle.mjs";

/** What each unit of the program changed: `[changed, initialized]` per unit. */
async function changes(source, options = {}) {
  const seen = await observe(source, { runs: 1, ...options });
  assert(!seen.invalid, seen.invalid);
  return { seen, units: seen.report.units.map((u) => [u.changed, u.initialized]) };
}

// ---- what the snapshot sees
const scenarios = [
  [
    "an alias, a primitive written with its own value, and an equal-looking new object",
    `
const a = { n: 0 };
const b = a;
a.n++;
let c = 1;
c = 1;
c = 2;
let x = { v: 1 };
x = { v: 1 };
`,
    [
      [[], ["a"]],
      [[], ["b"]],
      [["a", "b"], []],
      [[], ["c"]],
      [[], []],
      [["c"], []],
      [[], ["x"]],
      [["x"], []],
    ],
  ],
  [
    "a cycle, a symbol key, and an accessor: it is read by identity, never called",
    `
const s = Symbol("s");
const o = {};
o.self = o;
o[s] = 1;
let hidden = 0;
const acc = { get v() { return hidden; } };
hidden++;
`,
    [
      [[], ["s"]],
      [[], ["o"]],
      [["o"], []],
      [["o"], []],
      [[], ["hidden"]],
      [[], ["acc"]],
      [["hidden"], []],
    ],
  ],
  [
    "Map, Set, typed arrays, dates, regular expressions, arrays and attributes",
    `
const m = new Map([[1, { a: 1 }]]);
const set = new Set();
const ta = new Uint8Array(2);
const d = new Date(0);
const re = /a/g;
const arr = [1, 2];
const f = { a: 1 };
m.get(1).a = 2;
set.add(m);
ta[0] = 5;
d.setTime(1000);
re.lastIndex = 1;
arr.length = 1;
arr.push(3);
Object.freeze(f);
`,
    [
      [[], ["m"]],
      [[], ["set"]],
      [[], ["ta"]],
      [[], ["d"]],
      [[], ["re"]],
      [[], ["arr"]],
      [[], ["f"]],
      [["m"], []],
      [["set"], []],
      [["ta"], []],
      [["d"], []],
      [["re"], []],
      [["arr"], []],
      [["arr"], []],
      [["f"], []],
    ],
  ],
  [
    "the temporal dead zone: a function declaration is there from the start, a class and a let are not",
    `
function hoisted() { return later; }
class K { static n = 1; }
let later = 5;
K.n = 2;
`,
    [
      [[], []],
      [[], ["K"]],
      [[], ["later"]],
      [["K"], []],
    ],
  ],
];
await Promise.all(
  scenarios.map(async ([name, source, expected]) => {
    const { units } = await changes(source);
    assert.deepEqual(units, expected, name);
  })
);

// ---- what it reports besides bindings: output, globals, patched standard objects, continuations after the last unit
{
  const { seen } = await changes(`
console.log("x");
globalThis.G = { a: 1 };
Array.prototype.oracleProbe = 1;
let t = 0;
(async () => { await null; t = 5; })();
`);
  const [printed, global] = seen.report.units;
  assert(printed.printed > 0 && printed.changed.length === 0);
  assert.deepEqual(global.changed, ["<global>"]);
  assert(
    seen.report.patched.some((label) => label.startsWith("Array.prototype")),
    JSON.stringify(seen.report.patched)
  );
  assert.deepEqual(seen.report.tail, ["t"]);
}

// ---- inputs it refuses: a program that throws, does not finish, or does not give the same answer twice
{
  assert.match((await observe("const a = 1;\nnull.x;\n", { runs: 1 })).invalid, /throws \(premise P4\)/);
  assert.match((await observe("for (;;) {}\n", { runs: 1, timeoutMs: 500 })).invalid, /did not finish/);
  // the two runs are told apart by the name of the instrumented file
  const differs = 'let a = 0;\nif (import.meta.url.endsWith("oracle_0.mjs")) a++;\n';
  assert.match((await observe(differs, { runs: 2 })).invalid, /not deterministic/);
  assert(
    (await observe("const r = Math.random();\nconst s = { r };\n", { runs: 2 })).units.length === 2,
    "random values that change no binding are harmless"
  );
}

// ---- the units are the front end's: a declarator is a unit, an export is its declaration, an import and an export list are none
{
  const { units, names } = instrument(`
import { sep } from "node:path";
export const a = 1, b = 2;
export function f() {}
const z = [sep];
export { z as w };
`);
  assert.deepEqual(
    units.map((u) => u.source),
    ["const a = 1;", "const b = 2;", "function f() {}", "const z = [sep];"]
  );
  assert.deepEqual([...names].sort(), ["a", "b", "f", "z"]);
}

// a top-level await is refused, an await inside a function is not
{
  assert.throws(() => instrument("await null;\n"), /top-level await/);
  assert.throws(() => instrument("for await (const x of []) {}\n"), /top-level await/);
  assert.equal(
    instrument("async function f() { await null; }\nclass K { static { (async () => { await null; })(); } }\n").units
      .length,
    2
  );
}

// ---- the comparison with footprints
{
  const units = [
    { source: "u0" },
    { source: "u1" },
    { source: "u2" },
    { source: "u3" },
    { source: "u4" },
    { source: "u5" },
  ];
  const report = {
    units: [
      { changed: ["a"], initialized: [], printed: 0 },
      { changed: ["a"], initialized: [], printed: 0 },
      { changed: ["a"], initialized: [], printed: 0 },
      { changed: ["<global>"], initialized: [], printed: 0 },
      { changed: [], initialized: [], printed: 2 },
      { changed: [], initialized: ["b"], printed: 0 },
    ],
  };
  const fp = (defs, writes, effect) => ({ defs, writes, effect });
  const footprints = [
    fp([], ["a"], false),
    fp([], [], false),
    fp([], [], true),
    fp([], [], false),
    fp([], [], false),
    fp(["c"], [], false),
  ];
  assert.deepEqual(
    compare(units, footprints, report).map((f) => [f.unit, f.kind, f.name]),
    [
      [1, "unwritten", "a"],
      [2, "unlisted", "a"],
      [3, "global", "<global>"],
      [4, "printed", "2 writes"],
      [5, "foreign-definition", "b"],
    ]
  );
  // a name the unit declares or writes is fine, and so is a unit that is an effect and prints
  assert.deepEqual(compare(units.slice(0, 1), [fp(["a"], [], false)], { units: [report.units[0]] }), []);
  assert.deepEqual(compare([units[4]], [fp([], [], true)], { units: [report.units[4]] }), []);
}

// ---- end to end with the real analysis: no finding on a program whose footprints are right, and one when a write is erased
{
  const source = `
const a = { n: 0 };
const b = a;
function bump(t) { t.n++; }
bump(b);
const seen = a.n;
`;
  const result = await checkProgram(source, { runs: 1 });
  assert(!result.invalid, result.invalid);
  assert.equal(result.units, 5);
  assert.deepEqual(result.findings, []);
  assert.deepEqual(result.report.units[3].changed, ["a", "b"]);
  assert.deepEqual(result.footprints[3].writes.sort(), ["a", "b"]);
  const erased = result.footprints.map((f, i) => (i === 3 ? { ...f, writes: [] } : f));
  assert.deepEqual(
    compare(
      result.report.units.map((_, i) => ({ source: String(i) })),
      erased,
      result.report
    ).map((f) => [f.unit, f.kind, f.name]),
    [
      [3, "unwritten", "a"],
      [3, "unwritten", "b"],
    ]
  );
}
console.log("footprint oracle ok");
