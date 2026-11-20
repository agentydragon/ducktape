// Fuzzer for the translation checker: does `translate` ever accept a tree that behaves differently from its bundle?
//
//   node fuzz.mjs [--count N] [--seed S] [--trees T] [--jobs J] [--leaks] [--callback-corpus] [--ast-corpus] [--out DIR] [--keep] [--require-accepted]
//
// Each round generates a small random bundle from a catalog of unit templates (values, functions, classes, mutable
// counters, getters, iterators, globals, `var` hoisting, ...), then T random trees of ES modules for it: a random placement
// of units into modules, random order inside modules and between imports, random renames and import aliases. The
// generator knows what every unit declares and mentions, so its imports do not depend on the checker's scope analysis.
// Node runs the bundle and every tree. A tree the checker accepts (exit 0) must print the same as the bundle:
// an accepted tree that does not is a WRONG ACCEPT, and the exit code of this script is 1.
//
// `--leaks` adds templates whose behavior depends on something a move or a rename changes: `import.meta.url`, direct `eval`,
// top-level `await` (the checker must refuse these) and `fn.name` (assumed harmless: such wrong accepts are counted apart).
// Without it those are left out, so that the run looks for holes nobody listed. `fuzz_mutate.py` checks that the fuzzer
// notices a checker with one rule removed.
import { spawn } from "node:child_process";
import { cpSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { frontBundle, frontTree } from "./frontend.mjs";
import { generateAstBundle, astStatistics, printAst } from "./ast_generator.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const translate = process.env.TRANSLATE ?? path.join(here, "../../lean/.lake/build/bin/translate");

function rng(seed) {
  let a = seed >>> 0;
  const float = () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), a | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const int = (n) => Math.floor(float() * n);
  return {
    int,
    chance: (p) => float() < p,
    pick: (xs) => xs[int(xs.length)],
    shuffle: (xs) => {
      const out = [...xs];
      for (let i = out.length - 1; i > 0; i--) {
        const j = int(i + 1);
        [out[i], out[j]] = [out[j], out[i]];
      }
      return out;
    },
  };
}

// A unit's source mentions names through markers, so that one source renders under any naming.
const top = (n) => `\u0001${n}\u0002`; // a module-scope name, renamed and imported per module
const local = (n) => `\u0003${n}\u0004`; // a name local to the unit
const literal = (n) => `\u0005${n}\u0006`; // the bundle's spelling of a name, inside a string; the name is still used
const TOP = /\u0001([^\u0002]+)\u0002/g;
const LOCAL = /\u0003([^\u0004]+)\u0004/g;
const LITERAL = /\u0005([^\u0006]+)\u0006/g;
const mentioned = (src) => [...src.matchAll(TOP), ...src.matchAll(LITERAL)].map((m) => m[1]);

const LITERALS = ["1", "2", "7", '"s"', "[1, 2, 3]", "{ k: 1 }", "true", "null"];

const makeCounter = (g) => {
  const c = g.fresh("c");
  g.add("letCounter", `let ${top(c)} = 0;`, {
    defs: [c],
    regs: [{ name: c, kind: "let", num: true, mutable: true, json: true, data: true }],
  });
};
const makeIterable = (g) => {
  const it = g.fresh("it");
  g.add(
    "iterObject",
    `const ${top(it)} = { [Symbol.iterator]() { console.log("iter ${it}#${g.id()}"); return [1, 2][Symbol.iterator](); } };`,
    { defs: [it], regs: [{ name: it, kind: "const", iterable: true, data: true }] }
  );
};
const makeGetter = (g) => {
  const o = g.fresh("o");
  g.add("getterObject", `const ${top(o)} = { get v() { console.log("get ${o}#${g.id()}"); return 1; } };`, {
    defs: [o],
    regs: [{ name: o, kind: "const", getter: true, data: true }],
  });
};
const makeToString = (g) => {
  const t = g.fresh("t");
  g.add("toStringObject", `const ${top(t)} = { toString() { console.log("toString ${t}#${g.id()}"); return "t"; } };`, {
    defs: [t],
    regs: [{ name: t, kind: "const", strObject: true, data: true }],
  });
};
const makeClass = (g) => {
  const u = g.pickData();
  if (!u) return false;
  const c = g.fresh("C");
  const p = g.fresh("p");
  g.add(
    "classDecl",
    `class ${top(c)} { constructor(${local(p)}) { this.v = ${local(p)}; } get() { return this.v + ${top(u.name)}; } }`,
    { defs: [c], regs: [{ name: c, kind: "class", cls: true }] }
  );
};
const makeProp = (g) => {
  const o = g.fresh("o");
  g.add("propObject", `const ${top(o)} = { n: 0 };`, {
    defs: [o],
    regs: [{ name: o, kind: "const", prop: true, data: true }],
  });
};

/**
 * Catalog entries: [name, weight, leak, (g) => boolean]. `leak` is false for ordinary units, "refused" for a construct the
 * checker must refuse (an accepted tree that behaves differently is a wrong accept), and "assumed" for one the checker
 * accepts by assumption (a run with such a template reports its differences apart). `g.add` appends a unit; a template that
 * cannot apply returns false.
 */
const TEMPLATES = [
  [
    "constLit",
    3,
    false,
    (g) => {
      const v = g.fresh("v");
      const lit = g.r.pick(LITERALS);
      g.add("constLit", `const ${top(v)} = ${lit};`, {
        defs: [v],
        regs: [{ name: v, kind: "const", num: /^\d+$/.test(lit), json: true, data: true }],
      });
    },
  ],
  [
    "constAlias",
    2,
    false,
    (g) => {
      const u = g.pickValue();
      if (!u) return false;
      const v = g.fresh("v");
      g.add("constAlias", `const ${top(v)} = ${top(u.name)};`, { defs: [v], regs: [{ ...u, name: v, kind: "const" }] });
    },
  ],
  [
    "constObject",
    1,
    false,
    (g) => {
      const u = g.pickData();
      if (!u) return false;
      const v = g.fresh("v");
      g.add("constObject", `const ${top(v)} = { k: ${top(u.name)}, l: [${top(u.name)}] };`, {
        defs: [v],
        regs: [{ name: v, kind: "const", json: u.json, data: true }],
      });
    },
  ],
  [
    "constArith",
    2,
    false,
    (g) => {
      const u = g.pickData((e) => e.num);
      if (!u) return false;
      const v = g.fresh("v");
      g.add("constArith", `const ${top(v)} = ${top(u.name)} + 1;`, {
        defs: [v],
        regs: [{ name: v, kind: "const", num: true, json: true, data: true }],
      });
    },
  ],
  [
    "fnDecl",
    3,
    false,
    (g) => {
      const u = g.pickData();
      if (!u) return false;
      const f = g.fresh("f");
      const p = g.fresh("p");
      const body = g.r.chance(0.3)
        ? `function ${top(f)}(${local(p)} = ${top(u.name)}) { return ${local(p)}; }`
        : `function ${top(f)}(${local(p)}) { return ${local(p)} + ${top(u.name)}; }`;
      g.add("fnDecl", body, { defs: [f], regs: [{ name: f, kind: "fn", callable: true }] });
    },
  ],
  [
    "arrowConst",
    2,
    false,
    (g) => {
      const u = g.pickData((e) => e.num);
      if (!u) return false;
      const f = g.fresh("f");
      const p = g.fresh("p");
      g.add("arrowConst", `const ${top(f)} = (${local(p)}) => ${local(p)} * ${top(u.name)};`, {
        defs: [f],
        regs: [{ name: f, kind: "arrow", callable: true }],
      });
    },
  ],
  ["classDecl", 2, false, makeClass],
  [
    "classStaticInit",
    2,
    false,
    (g) => {
      const c = g.fresh("C");
      g.add("classStaticInit", `class ${top(c)} { static s = console.log("static ${c}#${g.id()}"); }`, {
        defs: [c],
        regs: [{ name: c, kind: "class" }],
      });
    },
  ],
  [
    "classStaticBlock",
    2,
    false,
    (g) => {
      const c = g.fresh("C");
      g.add("classStaticBlock", `class ${top(c)} { static { console.log("block ${c}#${g.id()}"); } }`, {
        defs: [c],
        regs: [{ name: c, kind: "class" }],
      });
    },
  ],
  [
    "classExtends",
    1,
    false,
    (g) => {
      const b = g.need((e) => e.cls, makeClass);
      if (!b) return false;
      const c = g.fresh("C");
      g.add("classExtends", `class ${top(c)} extends ${top(b.name)} {}`, {
        defs: [c],
        regs: [{ name: c, kind: "class", cls: true }],
      });
    },
  ],
  ["letCounter", 2, false, makeCounter],
  [
    "bumpFn",
    2,
    false,
    (g) => {
      const c = g.need((e) => e.mutable, makeCounter);
      if (!c) return false;
      const f = g.fresh("f");
      g.add("bumpFn", `function ${top(f)}() { ${top(c.name)}++; }`, {
        defs: [f],
        regs: [{ name: f, kind: "fn", callable: true }],
      });
    },
  ],
  [
    "assignStmt",
    3,
    false,
    (g) => {
      const c = g.need((e) => e.mutable, makeCounter);
      if (!c) return false;
      g.add("assignStmt", g.r.chance(0.5) ? `${top(c.name)} = ${g.r.int(9)};` : `${top(c.name)} += ${1 + g.r.int(3)};`);
    },
  ],
  [
    "readMutable",
    3,
    false,
    (g) => {
      const c = g.need((e) => e.mutable, makeCounter);
      if (!c) return false;
      const d = g.fresh("v");
      g.add(
        "readMutable",
        g.r.chance(0.5) ? `const ${top(d)} = ${top(c.name)};` : `const ${top(d)} = [${top(c.name)}];`,
        { defs: [d], regs: [{ name: d, kind: "const", json: true, data: true }] }
      );
    },
  ],
  [
    "logStmt",
    4,
    false,
    (g) => {
      const u = g.pickData();
      if (!u) return false;
      g.add("logStmt", `console.log("log#${g.id()}", ${top(u.name)});`);
    },
  ],
  [
    "callLog",
    2,
    false,
    (g) => {
      const f = g.pickValue((e) => e.callable);
      if (!f) return false;
      g.add("callLog", `console.log("call#${g.id()}", ${top(f.name)}(2));`);
    },
  ],
  [
    "callStmt",
    3,
    false,
    (g) => {
      const f = g.pickValue((e) => e.callable);
      if (!f) return false;
      g.add("callStmt", `${top(f.name)}(1);`);
    },
  ],
  [
    "newLog",
    1,
    false,
    (g) => {
      const c = g.need((e) => e.cls, makeClass);
      if (!c) return false;
      g.add("newLog", `console.log("new#${g.id()}", new ${top(c.name)}(3).get());`);
    },
  ],
  ["getterObject", 1, false, makeGetter],
  [
    "getterRead",
    2,
    false,
    (g) => {
      const o = g.need((e) => e.getter, makeGetter);
      if (!o) return false;
      const w = g.fresh("v");
      g.add("getterRead", `const ${top(w)} = ${top(o.name)}.v;`, {
        defs: [w],
        regs: [{ name: w, kind: "const", num: true, json: true, data: true }],
      });
    },
  ],
  ["iterObject", 1, false, makeIterable],
  [
    "spread",
    2,
    false,
    (g) => {
      const it = g.need((e) => e.iterable, makeIterable);
      if (!it) return false;
      const a = g.fresh("v");
      g.add("spread", `const ${top(a)} = [...${top(it.name)}];`, {
        defs: [a],
        regs: [{ name: a, kind: "const", json: true, data: true }],
      });
    },
  ],
  [
    "forOf",
    1,
    false,
    (g) => {
      const it = g.need((e) => e.iterable, makeIterable);
      if (!it) return false;
      const x = g.fresh("x");
      g.add("forOf", `for (const ${local(x)} of ${top(it.name)}) { console.log("x#${g.id()}", ${local(x)}); }`);
    },
  ],
  [
    "destructure",
    2,
    false,
    (g) => {
      // an array pattern runs the iterator, an object pattern runs the getter
      const fromGetter = g.r.chance(0.5);
      const source = fromGetter ? g.need((e) => e.getter, makeGetter) : g.need((e) => e.iterable, makeIterable);
      if (!source) return false;
      const a = g.fresh("v");
      const pattern = fromGetter ? `{ v: ${top(a)} }` : `[${top(a)}]`;
      g.add("destructure", `const ${pattern} = ${top(source.name)};`, {
        defs: [a],
        regs: [{ name: a, kind: "const", num: true, json: true, data: true }],
      });
    },
  ],
  ["toStringObject", 1, false, makeToString],
  [
    "templateUse",
    1,
    false,
    (g) => {
      const t = g.need((e) => e.strObject, makeToString);
      if (!t) return false;
      const s = g.fresh("v");
      g.add("templateUse", `const ${top(s)} = \`\${${top(t.name)}}\`;`, {
        defs: [s],
        regs: [{ name: s, kind: "const", json: true, data: true }],
      });
    },
  ],
  [
    "valueOfObject",
    1,
    false,
    (g) => {
      const o = g.fresh("o");
      const n = g.fresh("v");
      g.add("valueOfObject", `const ${top(o)} = { valueOf() { console.log("valueOf ${o}#${g.id()}"); return 1; } };`, {
        defs: [o],
        regs: [{ name: o, kind: "const", data: true }],
      });
      g.add("valueOfAdd", `const ${top(n)} = ${top(o)} + 1;`, {
        defs: [n],
        regs: [{ name: n, kind: "const", num: true, json: true, data: true }],
      });
    },
  ],
  [
    "globalSet",
    2,
    false,
    (g) => {
      const k = `G${g.id()}`;
      g.globals.push(k);
      g.add("globalSet", `globalThis.${k} = ${g.r.int(9)};`);
    },
  ],
  [
    "globalGet",
    2,
    false,
    (g) => {
      if (g.globals.length === 0) return false;
      g.add("globalGet", `console.log("global#${g.id()}", globalThis.${g.r.pick(g.globals)});`);
    },
  ],
  [
    "varBeforeDecl",
    1,
    false,
    (g) => {
      const x = g.fresh("x");
      g.add("varRead", `console.log("typeof#${g.id()}", typeof ${top(x)});`);
      g.add("varDecl", `var ${top(x)} = 1;`, {
        defs: [x],
        regs: [{ name: x, kind: "var", num: true, json: true, data: true }],
      });
    },
  ],
  [
    "blockVar",
    1,
    false,
    (g) => {
      const b = g.fresh("bv");
      g.add("blockVar", `{ var ${top(b)} = 3; }`, {
        defs: [b],
        regs: [{ name: b, kind: "var", num: true, json: true, data: true }],
      });
    },
  ],
  [
    "tryCatch",
    1,
    false,
    (g) => {
      const f = g.fresh("f");
      g.add("throwFn", `function ${top(f)}() { throw new Error("boom"); }`, {
        defs: [f],
        regs: [{ name: f, kind: "fn" }],
      });
      g.add("tryCatch", `try { ${top(f)}(); } catch (e) { console.log("caught#${g.id()}", e.message); }`);
    },
  ],
  [
    "loopUse",
    1,
    false,
    (g) => {
      const u = g.pickData();
      if (!u) return false;
      const i = g.fresh("i");
      g.add(
        "loopUse",
        `for (let ${local(i)} = 0; ${local(i)} < 2; ${local(i)}++) { console.log("loop#${g.id()}", ${local(i)}, ${top(u.name)}); }`
      );
    },
  ],
  [
    "ifUse",
    1,
    false,
    (g) => {
      const u = g.pickData();
      if (!u) return false;
      g.add("ifUse", `if (${top(u.name)}) { console.log("if#${g.id()}"); } else { console.log("else#${g.id()}"); }`);
    },
  ],
  [
    "forwardFn",
    1,
    false,
    (g) => {
      const a = g.fresh("f");
      const b = g.fresh("f");
      const p = g.fresh("p");
      g.add("forwardFn", `function ${top(a)}(${local(p)}) { return ${top(b)}(${local(p)}); }`, { defs: [a], regs: [] });
      g.add("forwardFnLeaf", `function ${top(b)}(${local(p)}) { return ${local(p)} + 1; }`, {
        defs: [b],
        regs: [
          { name: a, kind: "fn", callable: true },
          { name: b, kind: "fn", callable: true },
        ],
      });
    },
  ],
  [
    "asyncFn",
    2,
    false,
    (g) => {
      const f = g.fresh("f");
      const p = g.fresh("p");
      g.add(
        "asyncFn",
        `async function ${top(f)}(${local(p)}) { console.log("async start ${f}#${g.id()}"); await null; console.log("async end ${f}#${g.id()}", ${local(p)}); }`,
        { defs: [f], regs: [{ name: f, kind: "fn", callable: true }] }
      );
    },
  ],
  ["propObject", 1, false, makeProp],
  [
    "propWrite",
    3,
    false,
    (g) => {
      const o = g.need((e) => e.prop, makeProp);
      if (!o) return false;
      g.add("propWrite", `${top(o.name)}.n = ${g.r.int(9)};`);
    },
  ],
  [
    "propRead",
    3,
    false,
    (g) => {
      const o = g.pickValue((e) => e.prop);
      if (!o) return false;
      if (g.r.chance(0.5)) return g.add("propRead", `console.log("prop#${g.id()}", ${top(o.name)}.n);`);
      const v = g.fresh("v");
      g.add("propReadConst", `const ${top(v)} = ${top(o.name)}.n;`, {
        defs: [v],
        regs: [{ name: v, kind: "const", num: true, json: true, data: true }],
      });
    },
  ],
  [
    "classStaticWrite",
    2,
    false,
    (g) => {
      const o = g.need((e) => e.prop, makeProp);
      if (!o) return false;
      const c = g.fresh("C");
      g.add("classStaticWrite", `class ${top(c)} { static { ${top(o.name)}.n = ${g.r.int(9)}; } }`, {
        defs: [c],
        regs: [{ name: c, kind: "class" }],
      });
    },
  ],
  [
    "objectSpread",
    1,
    false,
    (g) => {
      const o = g.pickValue((e) => e.getter);
      if (!o) return false;
      const w = g.fresh("v");
      g.add("objectSpread", `const ${top(w)} = { ...${top(o.name)} };`, {
        defs: [w],
        regs: [{ name: w, kind: "const", data: true }],
      });
    },
  ],
  [
    "cellCluster",
    3,
    false,
    (g) => {
      // a counter, a function that bumps it, a call of that function and a pure read of the counter: the shape in which a
      // write made through a call can move past a reader
      const c = g.fresh("c");
      const f = g.fresh("f");
      const v = g.fresh("v");
      g.add("cellCounter", `let ${top(c)} = 0;`, {
        defs: [c],
        regs: [{ name: c, kind: "let", num: true, mutable: true, json: true, data: true }],
      });
      g.add("cellBump", `function ${top(f)}() { ${top(c)}++; }`, {
        defs: [f],
        regs: [{ name: f, kind: "fn", callable: true }],
      });
      g.add("cellCall", `${top(f)}();`);
      g.add("cellRead", `const ${top(v)} = ${top(c)};`, {
        defs: [v],
        regs: [{ name: v, kind: "const", num: true, json: true, data: true }],
      });
    },
  ],
  [
    "absoluteUrl",
    1,
    false,
    (g) => {
      const u = g.fresh("u");
      g.add("absoluteUrl", `const ${top(u)} = new URL("/static/${u}.js", import.meta.url).pathname;`, {
        defs: [u],
        regs: [{ name: u, kind: "const", json: true, data: true }],
      });
    },
  ],
  // reflection leaks: the behavior depends on a name, a position or a scheduling detail
  [
    "importMeta",
    1,
    "refused",
    (g) => g.add("importMeta", `console.log("meta#${g.id()}", import.meta.url.split("/").pop());`),
  ],
  [
    "fnName",
    1,
    "assumed",
    (g) => {
      const f = g.pickValue((e) => e.kind === "fn" || e.kind === "class" || e.kind === "arrow");
      if (!f) return false;
      g.add("fnName", `console.log("name#${g.id()}", ${top(f.name)}.name.length);`);
    },
  ],
  [
    "evalUse",
    1,
    "refused",
    (g) => {
      const u = g.pickData((e) => e.num);
      if (!u) return false;
      g.add("evalUse", `console.log("eval#${g.id()}", eval("${literal(u.name)} + 1"));`);
    },
  ],
  [
    "topLevelAwait",
    1,
    "refused",
    (g) => {
      g.add("tlaAwait", `await Promise.resolve();`);
      g.add("tlaLog", `console.log("tla#${g.id()}");`);
    },
  ],
];

function generate(r, leaks) {
  const g = {
    r,
    units: [],
    defined: [],
    globals: [],
    next: 0,
    fresh: (prefix) => `${prefix}${g.next++}`,
    id: () => g.units.length,
    add(tmpl, src, { defs = [], regs = [] } = {}) {
      g.units.push({ tmpl, src, defs });
      g.defined.push(...regs);
    },
    pickValue(pred = () => true) {
      const pool = g.defined.filter((e) => ["const", "let", "var", "fn", "arrow", "class"].includes(e.kind) && pred(e));
      return pool.length === 0 ? null : r.pick(pool);
    },
    /** An existing value with the property, or (often) a fresh one built by `maker`; null if there is none. */
    need(pred, maker) {
      const have = g.pickValue(pred);
      if (have && !r.chance(0.3)) return have;
      maker(g);
      return g.defined.findLast(pred) ?? null;
    },
    /** A value that can be printed or concatenated without exposing the name of a function or class. */
    pickData(pred = () => true) {
      return g.pickValue((e) => e.data && pred(e));
    },
  };
  const catalog = TEMPLATES.filter(([, , leak]) => leaks || !leak);
  const weighted = catalog.flatMap((t) => Array(t[1]).fill(t));
  const target = 8 + r.int(14);
  for (let tries = 0; g.units.length < target && tries < 200; tries++) r.pick(weighted)[3](g);
  const printable = g.defined.filter((e) => e.json && ["const", "let", "var"].includes(e.kind));
  const shown = r.shuffle(printable).slice(0, 6);
  g.add("finalReport", `console.log("final", JSON.stringify([${shown.map((e) => top(e.name)).join(", ")}]));`);
  const exportable = r.shuffle(g.defined.map((e) => e.name)).slice(0, r.int(4));
  for (const u of g.units) u.uses = [...new Set(mentioned(u.src))].filter((n) => !u.defs.includes(n));
  return { units: g.units, exports: [...new Set(exportable)] };
}

/** Two controlled schedules of a callback saved before an inferred call.
Tree 0 is a same-module control; tree 1 evaluates the callback before initialization.
The global and module-local storage variants exercise distinct escape paths. */
function callbackBundle(r, round, seed) {
  const factory = round % 4 >= 2;
  const alias = round % 2 === 1;
  const localSlot = round % 8 >= 4;
  const aliasForm = alias ? (round < 8 ? "alias" : seed % 2 ? "object" : "array") : "direct";
  const units = [];
  const side = [];
  const add = (tmpl, src, defs = [], inSide = false) => {
    if (inSide) side.push(units.length);
    units.push({ tmpl, src, defs });
  };
  const value = 1 + r.int(8);
  if (localSlot) {
    add("callbackSlot", `let ${top("slot")};`, ["slot"], true);
    add("callbackRegister", `function ${top("register")}(f) { ${top("slot")} = f; }`, ["register"], true);
    add("callbackTrigger", `function ${top("trigger")}() { return ${top("slot")}(); }`, ["trigger"], true);
  }
  if (factory) {
    add("closureFactory", `function ${top("factory")}(f) { return () => f(); }`, ["factory"]);
    add("callbackTarget", `function ${top("target")}() { return ${value}; }`, ["target"]);
  } else add("identityFunction", `function ${top("identity")}(x) { return x; }`, ["identity"]);
  if (round >= 8) add("shadow", `function ${top("shadow")}(result) { return result; }`, ["shadow"]);
  const observed =
    top(alias ? "alias" : "result") +
    (aliasForm === "object" ? ".value" : aliasForm === "array" ? "[0]" : "") +
    (factory ? "()" : "");
  if (localSlot) add("callbackEscape", `${top("register")}(() => ${observed});`, [], true);
  else add("callbackEscape", `globalThis.savedCallback = () => ${observed};`, [], true);
  add(
    "inferredCall",
    `const ${top("result")} = ${factory ? `${top("factory")}(${top("target")})` : `${top("identity")}(${value})`};`,
    ["result"]
  );
  if (alias) {
    const value =
      aliasForm === "object"
        ? `{value: ${top("result")}}`
        : aliasForm === "array"
          ? `[${top("result")}]`
          : top("result");
    add("callbackAlias", `const ${top("alias")} = ${value};`, ["alias"]);
  }
  add(
    "callbackInvoke",
    `console.log("value", ${localSlot ? `${top("trigger")}()` : "globalThis.savedCallback()"});`,
    [],
    true
  );
  for (const u of units) u.uses = [...new Set(mentioned(u.src))].filter((n) => !u.defs.includes(n));
  return {
    units,
    exports: [],
    side,
    caseName: `${factory ? "factory" : "identity"}/${aliasForm}/${localSlot ? "slot" : "global"}`,
  };
}

/** The bundle, one module, in the generator's spelling. */
function renderBundle(bundle) {
  const spell = (src) =>
    src
      .replace(TOP, (_, n) => n)
      .replace(LOCAL, (_, n) => n)
      .replace(LITERAL, (_, n) => n);
  const lines = bundle.units.map((u) => spell(u.src));
  if (bundle.exports.length > 0) lines.push(`export { ${bundle.exports.join(", ")} };`);
  return lines.join("\n") + "\n";
}

/** A random split of the bundle into modules (`entry.js`, `m1.js`, ...), with the unit mapping the checker's certificate needs. */
function split(bundle, r, opts) {
  const n = bundle.units.length;
  const modules = opts.place ? Math.max(...opts.place) + 1 : 1 + r.int(5);
  const effectful = (u) => u.tmpl !== "constLit" && !/^(const|function|class|let|var) /.test(u.src);
  const strategy = opts.place ? (opts.controlName ?? "callbackCorpus") : r.pick(["effectsInEntry", "random", "ranges"]);
  // the entry imports m1, m2, ... in ascending order and runs last, so range k of the bundle goes to module (k + 1) % modules
  const place = bundle.units.map((u, i) => {
    if (opts.place) return opts.place[i];
    if (modules === 1) return 0;
    if (strategy === "effectsInEntry") return effectful(u) ? 0 : r.int(modules);
    if (strategy === "ranges") return (Math.floor((i * modules) / n) + 1) % modules;
    return r.int(modules);
  });
  const order = Array.from({ length: modules }, () => []);
  place.forEach((m, i) => order[m].push(i));
  if (opts.moveBefore) {
    const list = order[0];
    const from = list.indexOf(opts.moveBefore.reader);
    const before = list.indexOf(opts.moveBefore.writer);
    if (from <= before || before < 0) throw new Error("invalid AST write-guided move");
    list.splice(from, 1);
    list.splice(before, 0, opts.moveBefore.reader);
  }
  // reorders inside a module: move a unit to another position, often next to a unit that mentions one of its names
  const names = (i) => new Set([...bundle.units[i].defs, ...bundle.units[i].uses]);
  const shares = (a, b) => [...names(a)].some((name) => names(b).has(name));
  const crowded = order.filter((list) => list.length > 1);
  for (let k = 0, moves = !opts.place && r.chance(0.6) ? 1 + r.int(3) : 0; k < moves && crowded.length > 0; k++) {
    const list = r.pick(crowded);
    let [from, to] = [r.int(list.length), r.int(list.length)];
    if (r.chance(0.6)) {
      const pairs = [];
      for (let x = 0; x < list.length; x++)
        for (let y = 0; y < list.length; y++) if (x !== y && shares(list[x], list[y])) pairs.push([x, y]);
      if (pairs.length > 0) [from, to] = r.pick(pairs);
    }
    list.splice(to, 0, ...list.splice(from, 1));
  }
  const owner = new Map();
  bundle.units.forEach((u, i) => u.defs.forEach((d) => owner.set(d, place[i])));
  const rename = !opts.place && r.chance(0.6);
  const taken = !opts.place && r.chance(0.06) ? r.pick([...owner.keys()]) : null; // renamed to a global that the units use
  const topName = (name) => (name === taken ? "JSON" : rename ? `r_${name}` : name);
  const localName = (name) => (opts.renameLocals ? `l_${name}` : name);
  const aliasOf = new Map();
  let capture = false;
  const nameIn = (m, name) => {
    if (owner.get(name) === m) return topName(name);
    const key = `${m}/${name}`;
    if (!aliasOf.has(key)) aliasOf.set(key, !opts.place && r.chance(0.3) ? `a${m}_${name}` : topName(name));
    return aliasOf.get(key);
  };
  if (!opts.place && r.chance(0.08)) {
    // an import alias that equals a local name of a unit that also uses the import: the local captures it
    search: for (const m of r.shuffle([...order.keys()])) {
      for (const i of order[m]) {
        const u = bundle.units[i];
        const locals = u.ast ? u.locals : [...new Set([...u.src.matchAll(LOCAL)].map((x) => x[1]))];
        const foreign = u.uses.filter((name) => owner.get(name) !== m);
        if (locals.length > 0 && foreign.length > 0) {
          aliasOf.set(`${m}/${foreign[0]}`, localName(locals[0]));
          capture = true;
          break search;
        }
      }
    }
  }
  const exported = new Set(bundle.exports);
  const external = (name) => (exported.has(name) && owner.get(name) === 0 ? name : `x_${name}`);
  const needs = Array.from({ length: modules }, () => new Map());
  const note = (m, name) => {
    const d = owner.get(name);
    if (d === m) return;
    if (!needs[m].has(d)) needs[m].set(d, new Set());
    needs[m].get(d).add(name);
  };
  bundle.units.forEach((u, i) => u.uses.forEach((name) => note(place[i], name)));
  bundle.exports.forEach((name) => note(0, name));
  const exportsOf = Array.from({ length: modules }, () => new Set());
  needs.forEach((byModule) => byModule.forEach((names, d) => names.forEach((name) => exportsOf[d].add(name))));
  bundle.exports.forEach((name) => owner.get(name) === 0 && exportsOf[0].add(name));
  const files = {};
  const mapping = [];
  const file = (m) => (m === 0 ? "entry.js" : `m${m}.js`);
  const used = order.map((list, m) => m === 0 || list.length > 0 || exportsOf[m].size > 0);
  for (let m = 0; m < modules; m++) {
    if (!used[m]) continue;
    const lines = [];
    const sources = new Set(needs[m].keys());
    if (m === 0) for (let k = 1; k < modules; k++) if (used[k]) sources.add(k);
    let importOrder = [...sources].sort((a, b) => a - b);
    if (!opts.place && r.chance(0.3)) importOrder = r.shuffle(importOrder);
    for (const d of importOrder) {
      const names = [...(needs[m].get(d) ?? [])];
      const specs = names.map((name) =>
        nameIn(m, name) === external(name) ? external(name) : `${external(name)} as ${nameIn(m, name)}`
      );
      lines.push(names.length === 0 ? `import "./${file(d)}";` : `import { ${specs.join(", ")} } from "./${file(d)}";`);
    }
    for (const i of order[m]) {
      const u = bundle.units[i];
      lines.push(
        u.ast
          ? printAst(u.ast, {
              top: (name) => (u.defs.includes(name) ? topName(name) : nameIn(m, name)),
              local: localName,
            })
          : u.src
              .replace(TOP, (_, name) => (u.defs.includes(name) ? topName(name) : nameIn(m, name)))
              .replace(LOCAL, (_, name) => localName(name))
              .replace(LITERAL, (_, name) => name)
      );
    }
    const out = [...exportsOf[m]].map((name) =>
      topName(name) === external(name) ? external(name) : `${topName(name)} as ${external(name)}`
    );
    const own = out.filter((_, k) => owner.get([...exportsOf[m]][k]) === m);
    if (m === 0)
      for (const name of bundle.exports)
        if (owner.get(name) !== 0) own.push(nameIn(0, name) === name ? name : `${nameIn(0, name)} as ${name}`);
    if (own.length > 0) lines.push(`export { ${own.join(", ")} };`);
    files[file(m)] = lines.join("\n") + "\n";
  }
  const paths = Object.keys(files).sort();
  for (const [mi, p] of paths.entries()) {
    const m = p === "entry.js" ? 0 : Number(p.slice(1, -3));
    order[m].forEach((i, u) => mapping.push([mi, u, i]));
  }
  return { files, mapping, strategy, capture: capture || taken !== null, modules: paths.length, rename };
}

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => child.kill("SIGKILL"), 15000);
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (status, signal) => {
      clearTimeout(timer);
      if (signal) {
        reject(new Error(`${command} ${args.join(" ")} terminated by ${signal}`));
        return;
      }
      resolve({ status, stdout, stderr });
    });
  });
}

const behavior = ({ status, stdout, stderr }) => ({
  status,
  stdout,
  error:
    status === 0 ? "" : (stderr.match(/\b(ReferenceError|TypeError|SyntaxError|RangeError|Error)\b/) ?? ["unknown"])[1],
});
const sameBehavior = (a, b) => a.status === b.status && a.stdout === b.stdout && a.error === b.error;

const checkerVerdicts = new Map([
  [0, { name: "accept", output: /^PLAN SOUND$/m }],
  [1, { name: "reject", output: /^(TRANSLATION REJECT|PLAN NOT SOUND):/m }],
  [4, { name: "obligations", output: /^OBLIGATIONS:/m }],
]);

async function main() {
  const args = process.argv.slice(2);
  const option = (name, fallback) => (args.includes(`--${name}`) ? args[args.indexOf(`--${name}`) + 1] : fallback);
  const count = Number(option("count", 100));
  const seed = Number(option("seed", 1));
  const trees = Number(option("trees", 6));
  const jobs = Number(option("jobs", Math.max(1, os.cpus().length)));
  const leaks = args.includes("--leaks");
  const callbackCorpus = args.includes("--callback-corpus");
  const astCorpus = args.includes("--ast-corpus");
  if (callbackCorpus && astCorpus) throw new Error("choose one structured corpus");
  const astTypes = {};
  const astFeatures = {};
  const planTypes = {};
  let astDepth = 0;
  const outDir = option("out", process.env.TEST_UNDECLARED_OUTPUTS_DIR);
  const work = mkdtempSync(path.join(process.env.TEST_TMPDIR ?? os.tmpdir(), "translation_fuzz_"));
  const tally = {};
  const wrongAccepts = [];
  const assumedAccepts = [];
  const wrongRejects = [];
  let invalidBundles = 0;
  let refused = 0;
  const bump = (key) => (tally[key] = (tally[key] ?? 0) + 1);

  const oneBundle = async (round) => {
    const r = rng(seed * 100003 + round);
    const bundle = astCorpus
      ? generateAstBundle(seed, round)
      : callbackCorpus
        ? callbackBundle(r, round, seed)
        : generate(r, leaks);
    if (astCorpus) {
      const stats = astStatistics(bundle);
      astDepth = Math.max(astDepth, stats.maxDepth);
      for (const [type, n] of Object.entries(stats.types)) astTypes[type] = (astTypes[type] ?? 0) + n;
      for (const [feature, n] of Object.entries(stats.features)) astFeatures[feature] = (astFeatures[feature] ?? 0) + n;
    }
    const dir = path.join(work, `b${round}`);
    mkdirSync(dir, { recursive: true });
    const bundleFile = path.join(dir, "bundle.mjs");
    writeFileSync(bundleFile, astCorpus ? bundle.source : renderBundle(bundle));
    const reference = behavior(await run(process.execPath, [bundleFile]));
    if (reference.status !== 0) {
      invalidBundles++;
      if (invalidBundles <= 3) console.log(`generator bug: bundle ${round} fails in Node (${reference.error})`);
      return;
    }
    const bundleJson = path.join(dir, "bundle.json");
    try {
      writeFileSync(bundleJson, JSON.stringify(frontBundle(bundleFile)));
    } catch (error) {
      refused++;
      return;
    }
    for (let t = 0; t < trees; t++) {
      const tr = rng(seed * 7919 + round * 131 + t);
      const place = callbackCorpus
        ? bundle.units.map((_, i) => (t % 2 === 1 ? Number(bundle.side.includes(i)) : round % 2))
        : astCorpus && t === 0
          ? bundle.units.map(() => 0)
          : astCorpus && t === 1 && bundle.guidedPlace
            ? bundle.guidedPlace
            : astCorpus && t === 2 && bundle.writeGuided
              ? bundle.units.map(() => 0)
              : undefined;
      const plan = split(bundle, tr, {
        renameLocals: !callbackCorpus && tr.chance(0.5),
        place,
        moveBefore: astCorpus && t === 2 ? bundle.writeGuided : undefined,
        controlName:
          astCorpus && t === 0
            ? "astControl"
            : astCorpus && t === 1 && bundle.guidedPlace
              ? "astGuided"
              : astCorpus && t === 2 && bundle.writeGuided
                ? "astWriteGuided"
                : undefined,
      });
      if (astCorpus) planTypes[plan.strategy] = (planTypes[plan.strategy] ?? 0) + 1;
      const treeDir = path.join(dir, `t${t}`, "out");
      mkdirSync(treeDir, { recursive: true });
      writeFileSync(path.join(treeDir, "package.json"), '{"type":"module"}');
      for (const [name, text] of Object.entries(plan.files)) writeFileSync(path.join(treeDir, name), text);
      const outputJson = path.join(dir, `t${t}`, "output.json");
      const certJson = path.join(dir, `t${t}`, "cert.json");
      try {
        writeFileSync(outputJson, JSON.stringify(frontTree(treeDir, "entry.js")));
      } catch (error) {
        refused++;
        continue;
      }
      writeFileSync(certJson, JSON.stringify({ pairs: plan.mapping }));
      const [checked, ran] = await Promise.all([
        run(translate, [bundleJson, outputJson, certJson]),
        run(process.execPath, [path.join(treeDir, "entry.js")]),
      ]);
      const tree = behavior(ran);
      const same = sameBehavior(reference, tree);
      const outcome = checkerVerdicts.get(checked.status);
      if (!outcome?.output.test(checked.stdout))
        throw new Error(
          `bundle ${round} tree ${t}: unexpected translate exit ${checked.status}\n${checked.stdout}${checked.stderr}`
        );
      const verdict = outcome.name;
      if (astCorpus && plan.strategy === "astGuided" && !same) bump("ast/guidedDifferent");
      if (astCorpus && plan.strategy === "astWriteGuided" && !same) {
        bump("ast/writeGuidedDifferent");
        if (verdict === "obligations") bump("ast/writeGuidedObligations");
      }
      if (
        astCorpus &&
        verdict === "accept" &&
        same &&
        (plan.modules > 1 || plan.mapping.some(([, , original], index) => original !== index))
      )
        bump("ast/nontrivialAccepted");
      if (astCorpus && /inferred identity call/.test(checked.stdout)) bump("ast/inferredIdentity");
      if (astCorpus && /inferred closure factory/.test(checked.stdout)) bump("ast/inferredFactory");
      if (astCorpus && t === 0 && (!same || verdict !== "accept"))
        throw new Error(`AST safe control ${round}: ${verdict}, same=${same}`);
      if (callbackCorpus && t % 2 === 0 && (!same || verdict !== "accept"))
        throw new Error(`${bundle.caseName}: safe control failed: ${verdict}, same=${same}`);
      if (callbackCorpus && t % 2 === 1 && same)
        throw new Error(`${bundle.caseName}: intended unsafe move did not change Node behavior`);
      if (callbackCorpus && t % 2 === 1 && verdict === "reject")
        throw new Error(`${bundle.caseName}: unsafe move was rejected for a structural reason: ${checked.stdout}`);
      if (callbackCorpus) bump(`${bundle.caseName}/${t % 2 ? "unsafe" : "safe"}`);
      const intent = plan.capture ? "capture" : "relocation";
      bump(`${verdict}/${same ? "same" : "differs"}`);
      const record = {
        seed,
        mode: astCorpus ? "ast" : callbackCorpus ? "callback" : "templates",
        round,
        tree: t,
        verdict,
        same,
        plan: { strategy: plan.strategy, modules: plan.modules, rename: plan.rename, capture: plan.capture },
        writeGuided: plan.strategy === "astWriteGuided" ? bundle.writeGuided : null,
        templates: [...new Set(bundle.units.map((u) => u.tmpl))].sort(),
        reference,
        observed: tree,
        checker: checked.stdout
          .split("\n")
          .filter((l) => /^(TRANSLATION REJECT|PLAN|OBLIGATIONS|\s+FAIL)/.test(l))
          .join(" | "),
      };
      if (verdict === "accept" && !same && record.templates.includes("fnName")) assumedAccepts.push(record);
      else if (verdict === "accept" && !same) {
        wrongAccepts.push(record);
        if (outDir) {
          const keep = path.join(outDir, `wrong_accept_${round}_${t}`);
          mkdirSync(keep, { recursive: true });
          cpSync(treeDir, path.join(keep, "out"), { recursive: true });
          cpSync(bundleFile, path.join(keep, "bundle.mjs"));
          cpSync(bundleJson, path.join(keep, "bundle.json"));
          cpSync(outputJson, path.join(keep, "output.json"));
          cpSync(certJson, path.join(keep, "cert.json"));
          if (astCorpus) writeFileSync(path.join(keep, "ast.json"), JSON.stringify(bundle.ast, null, 2));
          writeFileSync(path.join(keep, "record.json"), JSON.stringify(record, null, 2));
        }
      }
      if (verdict === "reject" && same && intent === "relocation") wrongRejects.push(record);
    }
  };

  let next = 0;
  await Promise.all(
    Array.from({ length: jobs }, async () => {
      while (next < count) await oneBundle(next++);
    })
  );
  if (args.includes("--keep") || (!outDir && (wrongAccepts.length > 0 || (astCorpus && invalidBundles > 0))))
    console.log(`kept ${work}`);
  else rmSync(work, { recursive: true, force: true });

  if (astCorpus && invalidBundles) throw new Error(`AST generator produced ${invalidBundles} invalid bundles`);
  if (astCorpus)
    console.log(
      "AST corpus:",
      JSON.stringify({
        seed,
        count,
        maxDepth: astDepth,
        types: astTypes,
        features: astFeatures,
        plans: planTypes,
        inferredIdentityPlans: tally["ast/inferredIdentity"] ?? 0,
        inferredFactoryPlans: tally["ast/inferredFactory"] ?? 0,
        guidedDifferentPlans: tally["ast/guidedDifferent"] ?? 0,
        writeGuidedDifferentPlans: tally["ast/writeGuidedDifferent"] ?? 0,
        writeGuidedObligations: tally["ast/writeGuidedObligations"] ?? 0,
        nontrivialAcceptedPlans: tally["ast/nontrivialAccepted"] ?? 0,
      })
    );
  if (callbackCorpus && (count < 1 || trees < 2))
    throw new Error("--callback-corpus needs at least one bundle and two trees");
  if (callbackCorpus && (invalidBundles || refused))
    throw new Error(`callback corpus incomplete: ${invalidBundles} invalid bundles; ${refused} refused trees`);
  if (callbackCorpus)
    console.log(
      "callback corpus:",
      JSON.stringify(
        Object.fromEntries(
          Object.entries(tally)
            .filter(([k]) => k.includes("/global/") || k.includes("/slot/"))
            .sort(([a], [b]) => a.localeCompare(b))
        )
      )
    );
  const cell = (v, s) => tally[`${v}/${s}`] ?? 0;
  if (astCorpus && args.includes("--require-ast-coverage")) {
    const required = [
      "FunctionDeclaration",
      "ArrowFunctionExpression",
      "IfStatement",
      "ConditionalExpression",
      "AssignmentExpression",
      "ObjectExpression",
      "ArrayExpression",
      "CallExpression",
    ];
    const missing = required.filter((name) => !astTypes[name]);
    if (
      missing.length ||
      astDepth < 6 ||
      !astFeatures.returnedClosures ||
      !astFeatures.callbackStores ||
      !astFeatures.callbackCalls ||
      !astFeatures.aliases ||
      !astFeatures.forwardReferences ||
      !astFeatures.earlyCallbacks ||
      cell("obligations", "differs") + cell("reject", "differs") === 0
    )
      throw new Error(
        `AST coverage incomplete: missing=${missing.join(",")} depth=${astDepth} features=${JSON.stringify(astFeatures)}`
      );
  }
  if (astCorpus && args.includes("--require-updates") && !astTypes.UpdateExpression)
    throw new Error("AST corpus did not exercise mutable updates");
  if (astCorpus && args.includes("--require-inferred-identity") && !tally["ast/inferredIdentity"])
    throw new Error("AST corpus did not exercise identity inference");
  if (astCorpus && args.includes("--require-inferred-factory") && !tally["ast/inferredFactory"])
    throw new Error("AST corpus did not exercise factory inference");
  console.log(
    `${count} bundles x ${trees} trees, seed ${seed}${leaks ? ", with leak templates" : ""}; ${invalidBundles} invalid bundles skipped, ${refused} trees the front end refused`
  );
  console.log("verdict       node same   node differs");
  for (const v of ["accept", "obligations", "reject"])
    console.log(`${v.padEnd(12)}  ${String(cell(v, "same")).padStart(9)}   ${String(cell(v, "differs")).padStart(12)}`);
  const byTemplate = {};
  for (const w of wrongAccepts) for (const t of w.templates) byTemplate[t] = (byTemplate[t] ?? 0) + 1;
  console.log(`WRONG ACCEPTS: ${wrongAccepts.length}`);
  for (const w of wrongAccepts.slice(0, 5))
    console.log(
      `  bundle ${w.round} tree ${w.tree} (${w.plan.strategy}, ${w.plan.modules} modules): ${JSON.stringify(w.reference.stdout.split("\n").slice(0, 3))} against ${JSON.stringify(w.observed.stdout.split("\n").slice(0, 3))} ${w.observed.error}`
    );
  if (wrongAccepts.length > 0)
    console.log(
      `  templates present in wrong accepts: ${Object.entries(byTemplate)
        .sort((a, b) => b[1] - a[1])
        .map(([t, k]) => `${t} ${k}`)
        .join(", ")}`
    );
  if (leaks) console.log(`accepted by the renaming assumption (bundles with fn.name): ${assumedAccepts.length}`);
  console.log(`wrong rejects of plain relocations (checker stricter than Node needed): ${wrongRejects.length}`);
  const reasons = {};
  for (const w of wrongRejects) reasons[w.checker.slice(0, 90)] = (reasons[w.checker.slice(0, 90)] ?? 0) + 1;
  for (const [reason, k] of Object.entries(reasons)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8))
    console.log(`  ${k}x ${reason}`);
  if (wrongAccepts.length > 0) throw new Error(`${wrongAccepts.length} wrong accepts`);
  if (args.includes("--require-accepted") && cell("accept", "same") === 0)
    throw new Error("no accepted tree matched its bundle");
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
