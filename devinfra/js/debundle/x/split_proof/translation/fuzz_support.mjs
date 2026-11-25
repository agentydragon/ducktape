// Deterministic program corpora and marker-based module renderer for fuzz.mjs.
// Keep generation and spelling rules here so the oracle can focus on checking behavior.
import { generate as printJs } from "astring";
import { generateAstBundle, astStatistics, generateRenamed } from "./testing/ast_generator.mjs";
import { generateProgram } from "./testing/gen/index.mjs";
import { seededRandom } from "../seeded_random.mjs";

// A unit's source mentions names through markers, so that one source renders under any naming.
export const top = (n) => `\u0001${n}\u0002`; // a module-scope name, renamed and imported per module
export const local = (n) => `\u0003${n}\u0004`; // a name local to the unit
export const literal = (n) => `\u0005${n}\u0006`; // the bundle's spelling of a name, inside a string; the name is still used
/* eslint-disable no-control-regex -- Match the generator’s reserved name markers. */
const TOP = /\u0001([^\u0002]+)\u0002/g;
const LOCAL = /\u0003([^\u0004]+)\u0004/g;
const LITERAL = /\u0005([^\u0006]+)\u0006/g;
/* eslint-enable no-control-regex */
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
// A function that writes `.n` of its argument, and a function that returns its argument: the helpers through which a
// write or an alias travels in the templates below.
const makeSetter = (g) => {
  const f = g.fresh("set");
  g.add("setterFn", `function ${top(f)}(t, v) { t.n = v; }`, {
    defs: [f],
    regs: [{ name: f, kind: "fn", setter: true }],
  });
};
const makeIdentity = (g) => {
  const f = g.fresh("id");
  g.add("identityFn", `function ${top(f)}(x) { return x; }`, {
    defs: [f],
    regs: [{ name: f, kind: "fn", identity: true }],
  });
};
const makeClosure = (g) => {
  const f = g.fresh("next");
  g.add("closureCounter", `const ${top(f)} = (() => { let n = 0; return () => ++n; })();`, {
    defs: [f],
    regs: [{ name: f, kind: "const", callable: true, closure: true }],
  });
};
const makeInstance = (g) => {
  const c = g.fresh("C");
  const i = g.fresh("inst");
  g.add("counterClass", `class ${top(c)} { n = 0; inc() { this.n++; return this.n; } }`, {
    defs: [c],
    regs: [{ name: c, kind: "class" }],
  });
  g.add("counterInstance", `const ${top(i)} = new ${top(c)}();`, {
    defs: [i],
    // printing an instance shows the name of its class, which a rename changes (the `fn.name` assumption): not `data`
    regs: [{ name: i, kind: "const", prop: true, instance: true, json: true }],
  });
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
  // ---- the bundle changes an intrinsic, and a unit reads what it changed
  [
    "intrinsicStatic",
    2,
    false,
    (g) => {
      const n = g.fresh("fz");
      const v = g.fresh("v");
      g.add("staticPatch", `Symbol.${n} = Symbol("${n}");`);
      g.add("staticRead", `const ${top(v)} = typeof Symbol.${n};`, {
        defs: [v],
        regs: [{ name: v, kind: "const", json: true, data: true }],
      });
    },
  ],
  [
    "intrinsicPrototype",
    2,
    false,
    (g) => {
      const n = g.fresh("fz");
      const v = g.fresh("v");
      const via = g.r.pick([`{}`, `[]`, `function () {}`, `new Map()`]);
      g.add("prototypePatch", `Object.prototype.${n} = "${n}";`);
      g.add("prototypeRead", `const ${top(v)} = typeof (${via}).${n};`, {
        defs: [v],
        regs: [{ name: v, kind: "const", json: true, data: true }],
      });
    },
  ],
  // ---- how a write and an alias can reach an object: the analysis must see every route
  [
    "aliasOf",
    4,
    false,
    (g) => {
      const o = g.pickValue((e) => e.prop);
      if (!o) return false;
      const a = g.fresh("a");
      const id = g.need((e) => e.identity, makeIdentity);
      const routes = [
        `{ k: ${top(o.name)} }.k`,
        `[${top(o.name)}][0]`,
        `new Map([["k", ${top(o.name)}]]).get("k")`,
        `(true ? ${top(o.name)} : { n: 9 })`,
        `(() => ${top(o.name)})()`,
        `{ self() { return ${top(o.name)}; } }.self()`,
        `(null ?? ${top(o.name)})`,
        `${top(id.name)}(${top(o.name)})`,
        `[1].map(() => ${top(o.name)})[0]`,
        `Object.assign({ z: 1 }, { w: ${top(o.name)} }).w`,
        `[${top(o.name)}].concat([])[0]`,
        `{ ...{ w: ${top(o.name)} } }.w`,
      ];
      const route = g.r.pick(routes);
      g.add("aliasOf", route.startsWith("{") ? `const ${top(a)} = (${route});` : `const ${top(a)} = ${route};`, {
        defs: [a],
        regs: [{ name: a, kind: "const", prop: true, instance: o.instance, json: true, data: !o.instance }],
      });
    },
  ],
  [
    "aliasViaDestructure",
    1,
    false,
    (g) => {
      const o = g.pickValue((e) => e.prop);
      if (!o) return false;
      const a = g.fresh("a");
      g.add("aliasViaDestructure", `const { k: ${top(a)} } = { k: ${top(o.name)} };`, {
        defs: [a],
        regs: [{ name: a, kind: "const", prop: true, instance: o.instance, json: true, data: !o.instance }],
      });
    },
  ],
  [
    "aliasViaRegistry",
    2,
    false,
    (g) => {
      const o = g.pickValue((e) => e.prop);
      if (!o) return false;
      const reg = g.fresh("reg");
      const a = g.fresh("a");
      g.add("registry", `const ${top(reg)} = {};`, { defs: [reg], regs: [{ name: reg, kind: "const", json: true }] });
      g.add("registryStore", `${top(reg)}.k = ${top(o.name)};`);
      g.add("registryRead", `const ${top(a)} = ${top(reg)}.k;`, {
        defs: [a],
        regs: [{ name: a, kind: "const", prop: true, instance: o.instance, json: true, data: !o.instance }],
      });
    },
  ],
  [
    "writeVia",
    5,
    false,
    (g) => {
      const o = g.pickValue((e) => e.prop);
      if (!o) return false;
      const k = 1 + g.r.int(8);
      const t = top(o.name);
      const set = g.need((e) => e.setter, makeSetter);
      const s = top(set.name);
      const forms = [
        `Object.assign(${t}, { n: ${k} });`,
        `[${k}].forEach((v) => { ${t}.n = v; });`,
        `(function (x) { x.n = ${k}; })(${t});`,
        `(() => { ${t}.n = ${k}; })();`,
        `Object.defineProperty(${t}, "n", { value: ${k}, writable: true, configurable: true, enumerable: true });`,
        `for (const v of [${k}]) ${t}.n = v;`,
        `try { ${t}.n = ${k}; } catch (e) {}`,
        `${t}.n++;`,
        `[${t}].map((x) => { x.n = ${k}; });`,
        `${s}(${t}, ${k});`,
        `${s}.call(null, ${t}, ${k});`,
        `${s}.apply(null, [${t}, ${k}]);`,
        `${s}.bind(null, ${t})(${k});`,
        `${s}(...[${t}, ${k}]);`,
        `(${k} > 0 ? ${t} : { n: 0 }).n = ${k};`,
        `({ w: ${t} }).w.n = ${k};`,
        `[${t}][0].n = ${k};`,
        `${t}["n"] = ${k};`,
        `(function* () { ${t}.n = ${k}; yield 1; })().next();`,
        `[...(function* () { ${t}.n = ${k}; yield 1; })()];`,
        `for (const _ of (function* () { ${t}.n = ${k}; yield 1; })()) {}`,
        // code the language runs implicitly: accessors, prototypes, conversions, iteration
        `({ get v() { ${t}.n = ${k}; return 1; } }).v;`,
        `({ set v(x) { ${t}.n = ${k}; } }).v = 1;`,
        `new (class { get v() { ${t}.n = ${k}; return 1; } })().v;`,
        `Object.setPrototypeOf({}, { m() { ${t}.n = ${k}; } }).m();`,
        `({ __proto__: { m() { ${t}.n = ${k}; } } }).m();`,
        `new Set({ [Symbol.iterator]() { ${t}.n = ${k}; return [][Symbol.iterator](); } });`,
        `Array.from({ [Symbol.iterator]() { ${t}.n = ${k}; return [][Symbol.iterator](); } });`,
        `Math.floor({ valueOf() { ${t}.n = ${k}; return 1; } });`,
        `[{ toString() { ${t}.n = ${k}; return "s"; } }].join();`,
        `JSON.stringify({ toJSON() { ${t}.n = ${k}; return 1; } });`,
        `void (({ [Symbol.toPrimitive]() { ${t}.n = ${k}; return 1; } }) + 1);`,
        `void (1 instanceof class { static [Symbol.hasInstance]() { ${t}.n = ${k}; return true; } });`,
      ];
      g.add("writeVia", g.r.pick(forms));
    },
  ],
  [
    "readVia",
    4,
    false,
    (g) => {
      const o = g.pickValue((e) => e.prop);
      if (!o) return false;
      const t = top(o.name);
      const v = g.fresh("v");
      const forms = [
        `(({ n }) => n)(${t})`,
        `Object.values(${t})[0]`,
        `[${t}].map((x) => x.n)[0]`,
        `(() => ${t}.n)()`,
        `Object.entries(${t})[0][1]`,
        `{ ...${t} }.n`,
        `[...[${t}]][0].n`,
        `(${t}.n ?? 0)`,
      ];
      const f = g.r.pick(forms);
      g.add("readVia", `const ${top(v)} = ${f.startsWith("{") ? `(${f})` : f};`, {
        defs: [v],
        regs: [{ name: v, kind: "const", num: true, json: true, data: true }],
      });
    },
  ],
  ["closureCounter", 2, false, makeClosure],
  [
    "closureUse",
    3,
    false,
    (g) => {
      const f = g.need((e) => e.closure, makeClosure);
      if (!f) return false;
      if (g.r.chance(0.5)) return g.add("closureCall", `${top(f.name)}();`);
      const v = g.fresh("v");
      g.add("closureConst", `const ${top(v)} = ${top(f.name)}();`, {
        defs: [v],
        regs: [{ name: v, kind: "const", num: true, json: true, data: true }],
      });
    },
  ],
  // A generator body runs when the iterator is advanced, in a later unit than the one that created it.
  [
    "generatorIterator",
    3,
    false,
    (g) => {
      const o = g.pickValue((e) => e.prop);
      if (!o) return false;
      const it = g.fresh("gen");
      g.add(
        "generatorCreate",
        `const ${top(it)} = (function* () { ${top(o.name)}.n = ${1 + g.r.int(8)}; yield 1; })();`,
        {
          defs: [it],
          regs: [{ name: it, kind: "const", genIter: true }],
        }
      );
      g.add("generatorAdvance", `${top(it)}.next();`);
    },
  ],
  ["counterInstance", 2, false, makeInstance],
  [
    "instanceUse",
    4,
    false,
    (g) => {
      const i = g.need((e) => e.instance, makeInstance);
      if (!i) return false;
      if (g.r.chance(0.5)) return g.add("instanceInc", `${top(i.name)}.inc();`);
      const v = g.fresh("v");
      g.add("instanceConst", `const ${top(v)} = ${top(i.name)}.inc();`, {
        defs: [v],
        regs: [{ name: v, kind: "const", num: true, json: true, data: true }],
      });
    },
  ],
  [
    "decorate",
    2,
    false,
    (g) => {
      // the shape of a decorator helper: a function that defines a property of its argument, called on a prototype or an object
      const o = g.pickValue((e) => e.prop);
      if (!o) return false;
      const d = g.fresh("deco");
      g.add(
        "decoFn",
        `function ${top(d)}(t, k) { Object.defineProperty(t, k, { value: 1, writable: true, configurable: true, enumerable: true }); }`,
        {
          defs: [d],
          regs: [{ name: d, kind: "fn" }],
        }
      );
      g.add("decoCall", `${top(d)}(${top(o.name)}, "z");`);
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
export function renderBundle(bundle) {
  const spell = (src) =>
    src
      .replace(TOP, (_, n) => n)
      .replace(LOCAL, (_, n) => n)
      .replace(LITERAL, (_, n) => n);
  const lines = [...(bundle.imports ?? []), ...bundle.units.map((u) => spell(u.src))];
  if (bundle.exports.length > 0) lines.push(`export { ${bundle.exports.join(", ")} };`);
  return lines.join("\n") + "\n";
}

const identifier = (name) => ({ type: "Identifier", name });

function exportSpecifier(local, exported) {
  return { type: "ExportSpecifier", local: identifier(local), exported: identifier(exported) };
}

/** A random split of the bundle into modules (`entry.js`, `m1.js`, ...), with the unit mapping the checker's certificate needs. */
export function split(bundle, r, opts) {
  const n = bundle.units.length;
  const modules = opts.place ? Math.max(...opts.place) + 1 : 1 + r.int(5);
  const effectful = (unit) =>
    unit.ast
      ? !["VariableDeclaration", "FunctionDeclaration", "ClassDeclaration"].includes(unit.ast.type)
      : unit.tmpl !== "constLit" && !/^(const|function|class|let|var) /.test(unit.src);
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
    const lines = [...(bundle.imports ?? [])]; // imports of the program are every module's own
    const sources = new Set(needs[m].keys());
    if (m === 0) for (let k = 1; k < modules; k++) if (used[k]) sources.add(k);
    let importOrder = [...sources].sort((a, b) => a - b);
    if (!opts.place && r.chance(0.3)) importOrder = r.shuffle(importOrder);
    for (const d of importOrder) {
      const names = [...(needs[m].get(d) ?? [])];
      const specifiers = names.map((name) => ({
        type: "ImportSpecifier",
        imported: identifier(external(name)),
        local: identifier(nameIn(m, name)),
      }));
      lines.push(
        printJs({ type: "ImportDeclaration", specifiers, source: { type: "Literal", value: `./${file(d)}` } })
      );
    }
    for (const i of order[m]) {
      const u = bundle.units[i];
      lines.push(
        u.ast
          ? generateRenamed(u.ast, {
              top: (name) => (u.defs.includes(name) ? topName(name) : nameIn(m, name)),
              local: localName,
            })
          : u.src
              .replace(TOP, (_, name) => (u.defs.includes(name) ? topName(name) : nameIn(m, name)))
              .replace(LOCAL, (_, name) => localName(name))
              .replace(LITERAL, (_, name) => name)
      );
    }
    const specifiers = [...exportsOf[m]]
      .filter((name) => owner.get(name) === m)
      .map((name) => exportSpecifier(topName(name), external(name)));
    if (m === 0)
      for (const name of bundle.exports)
        if (owner.get(name) !== 0) specifiers.push(exportSpecifier(nameIn(0, name), name));
    if (specifiers.length > 0)
      lines.push(printJs({ type: "ExportNamedDeclaration", declaration: null, specifiers, source: null }));
    files[file(m)] = lines.join("\n") + "\n";
  }
  const paths = Object.keys(files).sort();
  for (const [mi, p] of paths.entries()) {
    const m = p === "entry.js" ? 0 : Number(p.slice(1, -3));
    order[m].forEach((i, u) => mapping.push([mi, u, i]));
  }
  return { files, mapping, strategy, capture: capture || taken !== null, modules: paths.length, rename };
}

export { astStatistics };

/** Build one corpus program using the same seed and source family selection as the oracle. */
export function generateCorpusBundle({ seed, round, astCorpus, callbackCorpus, fullCorpus, leaks, families }) {
  const r = seededRandom(seed * 100003 + round);
  return astCorpus
    ? generateAstBundle(seed, round)
    : callbackCorpus
      ? callbackBundle(r, round, seed)
      : fullCorpus
        ? generateProgram(seed * 100003 + round, families ? { families } : {})
        : generate(r, leaks);
}
