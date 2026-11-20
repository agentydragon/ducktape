// Cases for the effect analysis: a small bundle, and for every top-level unit what the analysis must say.
//
//   `world`          the unit touches the environment or runs code the analysis cannot see
//   `pure`           it changes nothing but what it declares
//   `writes a b`     it may change the objects behind these other names, and nothing else
//
// Every expectation here was checked against what JavaScript does. A case whose expectation is `world` where the code
// is in fact harmless is a loss of precision, listed as such; one that says `pure` where the code writes something is a
// bug, and the fuzzer (`../translation/fuzz.mjs`) looks for those.
export const premises = {
  vendor: { "./v.js#make": "kit", "./v.js#kit": "kit" },
  members: { kit: ["extend", "optional"] },
  noWorldHooks: true,
};

export const cases = [
  {
    name: "values and aliases",
    source: `
const a = { n: 0 };
const b = a;
const c = [a, b];
function f() { return 1; }
const d = () => a;
class K { m() { return this; } }
const s = Symbol("s");
const m = new Map();
const re = /x/;
`,
    expect: ["pure", "pure", "pure", "pure", "pure", "pure", "pure", "pure", "pure"],
  },
  {
    name: "writes through aliases reach every name of the object",
    source: `
const a = { n: 0 };
const b = a;
const holder = { k: a };
b.n = 1;
holder.k.n = 2;
`,
    expect: ["pure", "pure", "pure", "writes a b holder", "writes a b holder"],
  },
  {
    name: "a helper writes its argument: each call writes only its own",
    source: `
const p = {};
const q = {};
function set(t, v) { t.v = v; }
set(p, 1);
set(q, 2);
`,
    expect: ["pure", "pure", "pure", "writes p", "writes q"],
  },
  {
    name: "a closure keeps state that its callers change",
    source: `
const next = (() => { let n = 0; return () => ++n; })();
const x = next();
const y = next();
`,
    expect: ["pure", "writes next", "writes next"],
  },
  {
    name: "a class: the constructor and a method write `this`",
    source: `
class Box { n = 0; inc() { this.n++; return this.n; } }
const box = new Box();
const other = new Box();
box.inc();
`,
    expect: ["pure", "pure", "pure", "writes box"],
  },
  {
    name: "the world: the console, a global, a call of something unknown",
    source: `
console.log("x");
window.title = "t";
unknownGlobal();
`,
    expect: ["world", "world", "world"],
  },
  {
    name: "the standard library: mutators write their object, the rest allocate",
    source: `
const o = { n: 0 };
const list = [];
const m = new Map();
Object.assign(o, { n: 1 });
Object.defineProperty(o, "k", { value: 1, writable: true, configurable: true });
list.push(o);
m.set("k", o);
const keys = Object.keys(o);
const copy = { ...o };
const str = JSON.stringify(o);
`,
    // `list` and `m` hold `o` by the end of the program, so a change of `o` is a change of what they hold
    expect: [
      "pure",
      "pure",
      "pure",
      "writes list m o",
      "writes list m o",
      "writes list",
      "writes m",
      "pure",
      "pure",
      "world",
    ],
  },
  {
    name: "callbacks, call, apply and bind run the function",
    source: `
const t = { n: 0 };
const u = { n: 0 };
function bump(x) { x.n++; }
[1].forEach(() => { t.n++; });
bump.call(null, u);
bump.apply(null, [t]);
const bound = bump.bind(null, u);
bound();
`,
    // `bound` holds `u` as an argument, so a change of `u` is a change of what `bound` holds
    expect: ["pure", "pure", "pure", "writes t", "writes bound u", "writes t", "pure", "writes bound u"],
  },
  {
    name: "accessors and conversion hooks run code",
    source: `
const g = { get v() { console.log("g"); return 1; } };
const r = g.v;
const h = { valueOf() { return 1; } };
const s = h + 1;
const w = { valueOf() { window.x = 1; return 1; } };
const z = w + 1;
`,
    expect: ["pure", "world", "pure", "pure", "pure", "world"],
  },
  {
    name: "a handler the world calls may assign a variable",
    source: `
let ready = false;
window.addEventListener("load", () => { ready = true; });
const r = ready;
`,
    expect: ["pure", "world", "pure"],
  },
  {
    name: "a conditional alias, a loop and a pattern write what they may reach",
    source: `
const a = { n: 0 };
const b = { n: 0 };
const p = Math.max(1, 2) > 1 ? a : b;
p.n = 1;
for (const x of [a, b]) x.n++;
const { n } = a;
`,
    expect: ["pure", "pure", "pure", "writes a b p", "writes a b p", "pure"],
  },
  {
    name: "a generator's body runs when the iterator is advanced, not when it is created",
    source: `
const o = { n: 0 };
const gen = (function* () { o.n = 5; yield 1; })();
gen.next();
const v = o.n === 5 ? "five" : "zero";
console.log(v);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "an async function resumes after the program: two calls keep the order of their continuations",
    source: `
const box = {};
const log = [];
async function a() { await null; box.v.push("a"); }
async function b() { await null; box.v.push("b"); }
a();
b();
box.v = log;
Promise.resolve().then(() => console.log(log.join()));
`,
    reorders: [{ order: [0, 1, 2, 3, 5, 4, 6, 7], behavior: "differs" }],
  },
  {
    name: "a call deeper than the analysis follows still runs the callee",
    source: `
const o = { n: 0 };
const f9 = () => { o.n = 1; };
const f8 = () => { f9(); };
const f7 = () => { f8(); };
const f6 = () => { f7(); };
const f5 = () => { f6(); };
const f4 = () => { f5(); };
const f3 = () => { f4(); };
const f2 = () => { f3(); };
const f1 = () => { f2(); };
f1();
const v = o.n === 1 ? "one" : "zero";
console.log(v);
`,
    reorders: [{ order: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 10, 12], behavior: "differs" }],
  },
  {
    name: "a call through more functions than the analysis follows still runs one of them",
    source: `
const o = { n: 0 };
const fs = [() => 0, () => 1, () => 2, () => 3, () => 4, () => 5, () => 6, () => 7, () => 8, () => 9, () => 10, () => 11, () => 12, () => 13, () => 14, () => 15, () => 16, () => 17, () => 18, () => 19, () => 20, () => { o.n = 1; }];
const pick = fs[Date.now() > 0 ? 21 : 0];
pick();
const v = o.n === 1 ? "one" : "zero";
console.log(v);
`,
    reorders: [{ order: [0, 1, 2, 4, 3, 5], behavior: "differs" }],
  },
  {
    name: "constructors nested deeper than the analysis follows still run",
    source: `
const o = { n: 0 };
class C9 { constructor() { o.n = 1; } }
class C8 { constructor() { new C9(); } }
class C7 { constructor() { new C8(); } }
class C6 { constructor() { new C7(); } }
class C5 { constructor() { new C6(); } }
class C4 { constructor() { new C5(); } }
class C3 { constructor() { new C4(); } }
class C2 { constructor() { new C3(); } }
class C1 { constructor() { new C2(); } }
new C1();
const v = o.n === 1 ? "one" : "zero";
console.log(v);
`,
    reorders: [{ order: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 10, 12], behavior: "differs" }],
  },
  {
    name: "instanceof runs the right operand's Symbol.hasInstance",
    source: `
const o = { n: 0 };
class Odd { static [Symbol.hasInstance](x) { o.n = 1; return true; } }
const t = 1 instanceof Odd;
const v = o.n === 1 ? "one" : "zero";
console.log(v);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "state of a module outside the tree: a write and a read keep their order",
    source: `
import { lib } from "%VENDOR%";
lib.settings.debug = true;
const d = lib.settings.debug ? "on" : "off";
console.log(d);
`,
    reorders: [{ order: [0, 2, 1, 3], behavior: "differs" }],
  },
  {
    name: "a handler assigns a variable: the unit that dispatches the event and a branch on the variable keep their order",
    source: `
let ready = false;
const cfg = { mode: "a" };
const et = new EventTarget();
et.addEventListener("go", () => { ready = true; });
if (ready) { cfg.mode = "b"; }
et.dispatchEvent(new Event("go"));
console.log(cfg.mode);
`,
    reorders: [{ order: [0, 1, 2, 3, 5, 4, 6], behavior: "differs" }],
  },
  {
    name: "a getter runs when the property is read",
    source: `
const o = { n: 0 };
const g = { get v() { o.n = 1; return 1; } };
const r = g.v;
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "a setter runs when the property is assigned",
    source: `
const o = { n: 0 };
const g = { set v(x) { o.n = x; } };
g.v = 1;
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "a class accessor, on the prototype and static, runs when read",
    source: `
const o = { n: 0 };
class K { get v() { o.n = 1; return 1; } static get s() { o.m = 2; return 2; } }
const k = new K();
const r = k.v;
const t = K.s;
const w = (o.n === 1 ? "one" : "zero") + (o.m === 2 ? "two" : "none");
console.log(w);
`,
    reorders: [
      { order: [0, 1, 2, 3, 5, 4, 6], behavior: "differs" },
      { order: [0, 1, 2, 5, 3, 4, 6], behavior: "differs" },
    ],
  },
  {
    name: "an accessor defined with defineProperty runs when read",
    source: `
const o = { n: 0 };
const g = {};
Object.defineProperty(g, "v", { get() { o.n = 1; return 1; } });
const r = g.v;
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 2, 4, 3, 5], behavior: "differs" }],
  },
  {
    name: "a method found on a prototype set with __proto__ in a literal runs",
    source: `
const o = { n: 0 };
const base = { m() { o.n = 1; } };
const d = { __proto__: base };
d.m();
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 2, 4, 3, 5], behavior: "differs" }],
  },
  {
    name: "a method found on a prototype set with Object.setPrototypeOf runs",
    source: `
const o = { n: 0 };
const base = { m() { o.n = 1; } };
const d = {};
Object.setPrototypeOf(d, base);
d.m();
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 2, 3, 5, 4, 6], behavior: "differs" }],
  },
  {
    name: "a method found on a prototype set by assigning __proto__ runs",
    source: `
const o = { n: 0 };
const base = { m() { o.n = 1; } };
const d = {};
d.__proto__ = base;
d.m();
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 2, 3, 5, 4, 6], behavior: "differs" }],
  },
  {
    name: "the prototype of an instance is the class's: a patch reaches every instance",
    source: `
const o = { n: 0 };
class K { m() { return 1; } }
const k = new K();
Object.getPrototypeOf(k).m = () => { o.n = 1; return 2; };
const r = k.m();
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 2, 4, 3, 5, 6], behavior: "differs" }],
  },
  {
    name: "Object.fromEntries holds the values of its entries",
    source: `
const objA = { n: 0 };
const r = Object.fromEntries([["a", objA]]);
r.a.n = 1;
const w = objA.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "new Set iterates an iterable the program defined",
    source: `
const o = { n: 0 };
class It { [Symbol.iterator]() { o.n = 1; return [1][Symbol.iterator](); } }
const it = new It();
const s = new Set(it);
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 2, 4, 3, 5], behavior: "differs" }],
  },
  {
    name: "Array.from iterates an iterable the program defined",
    source: `
const o = { n: 0 };
class It { [Symbol.iterator]() { o.n = 1; return [1][Symbol.iterator](); } }
const it = new It();
const s = Array.from(it);
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 2, 4, 3, 5], behavior: "differs" }],
  },
  {
    name: "++ converts its operand: valueOf runs",
    source: `
const o = { n: 0 };
let x = { valueOf() { o.n = 1; return 1; } };
x++;
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "a standard function converts its arguments: valueOf runs",
    source: `
const o = { n: 0 };
const x = { valueOf() { o.n = 1; return 1.5; } };
const m = Math.floor(x);
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "String(x) and join convert: toString runs",
    source: `
const o = { n: 0 };
const x = { toString() { o.n = 1; return "x"; } };
const m = [x].join();
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "a JSON.parse reviver runs",
    source: `
const o = { n: 0 };
const p = JSON.parse("[1]", (k, v) => { o.n = 1; return v; });
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 2, 1, 3], behavior: "differs" }],
  },
  {
    name: "converting an array of callbacks calls none of them",
    source: `
const o = { n: 0 };
const hs = [];
hs.push(() => { o.n = 1; });
hs.push(() => { o.n = 2; });
const w = o.n === 0 ? "zero" : "other";
console.log(w);
`,
    reorders: [{ order: [0, 1, 4, 2, 3, 5], behavior: "same", accepted: true }],
  },
  {
    name: "a variable the world's handler reads: what is assigned to it afterwards is the world's too",
    source: `
let x = { n: 0 };
const target = { n: 0 };
const et = new EventTarget();
et.addEventListener("go", () => { x.n = 1; });
x = target;
const before = target.n;
et.dispatchEvent(new Event("go"));
console.log(before, target.n);
`,
    reorders: [{ order: [0, 1, 2, 3, 4, 6, 5, 7], behavior: "differs" }],
  },
  {
    name: "resolving a thenable runs its then after the program, in the order they were resolved",
    source: `
const log = [];
const t1 = { then(r) { log.push("t1"); r(1); } };
const t2 = { then(r) { log.push("t2"); r(2); } };
Promise.resolve(t1);
Promise.resolve(t2);
Promise.resolve().then(() => console.log(log.join()));
`,
    reorders: [{ order: [0, 1, 2, 4, 3, 5], behavior: "differs" }],
  },
  {
    name: "a promise executor that resolves a thenable keeps the order",
    source: `
const log = [];
const t1 = { then(r) { log.push("t1"); r(1); } };
const t2 = { then(r) { log.push("t2"); r(2); } };
new Promise((res) => res(t1));
new Promise((res) => res(t2));
Promise.resolve().then(() => console.log(log.join()));
`,
    reorders: [{ order: [0, 1, 2, 4, 3, 5], behavior: "differs" }],
  },
  {
    name: "Symbol.toPrimitive runs when an operator converts",
    source: `
const o = { n: 0 };
const x = { [Symbol.toPrimitive]() { o.n = 1; return 1; } };
const y = x + 1;
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "spreading an iterable the program defined runs its iterator",
    source: `
const o = { n: 0 };
const it = { [Symbol.iterator]() { o.n = 1; return [1][Symbol.iterator](); } };
const arr = [...it];
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "JSON.stringify runs toJSON",
    source: `
const o = { n: 0 };
const x = { toJSON() { o.n = 1; return 1; } };
const s = JSON.stringify(x);
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "accessors the program defined are run, not given up on",
    source: `
const o = { n: 0 };
const g = { get v() { o.n = 1; return 2; }, set w(x) { o.m = x; } };
const r = g.v;
g.w = 3;
class K { get z() { return o.n; } }
const k = new K();
const z = k.z;
`,
    expect: ["pure", "pure", "writes o", "writes g o", "pure", "pure", "pure"],
  },
  {
    name: "a property of an object the world holds is read like any other (premise P3)",
    source: `
const cfg = { mode: "a" };
window.addEventListener("x", () => cfg.mode);
const m = cfg.mode;
cfg.mode = "b";
`,
    expect: ["pure", "world", "pure", "writes cfg"],
  },
  {
    name: "a getter the world made runs code the analysis cannot see",
    source: `
import { mk } from "%VENDOR%";
const o = { n: 0 };
globalThis.__cb = () => { o.n = 1; return 1; };
const g = {};
Object.defineProperty(g, "v", mk());
const r = g.v;
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 2, 3, 4, 6, 5, 7], behavior: "differs" }],
  },
  {
    name: "a handler the world calls reads a variable that a later unit initializes",
    source: `
const et = new EventTarget();
et.addEventListener("go", () => { console.log(typeof late); });
const late = 5;
et.dispatchEvent(new Event("go"));
`,
    reorders: [{ order: [0, 1, 3, 2], behavior: "differs" }],
  },
  {
    name: "an object a handler assigns to a variable: writing it through another name is unknown code",
    source: `
let h = null;
const et = new EventTarget();
et.addEventListener("go", () => { h = { n: 0 }; });
et.dispatchEvent(new Event("go"));
const t = h;
t.n = 1;
const w = h.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 2, 3, 4, 6, 5, 7], behavior: "differs" }],
  },
  {
    name: "a method called through a Map value",
    source: `
const o = { n: 0 };
const m = new Map([["k", () => { o.n = 1; }]]);
m.get("k")();
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "a callback stored in an array and called by forEach",
    source: `
const o = { n: 0 };
const hs = [() => { o.n = 1; }];
hs.forEach((h) => h());
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "a static method called through this in a static block",
    source: `
const o = { n: 0 };
class K { static m() { o.n = 1; } static { K.m(); } }
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 2, 1, 3], behavior: "differs" }],
  },
  {
    name: "a class field initializer with an effect",
    source: `
const o = { n: 0 };
class K { f = (o.n = 1); }
const k = new K();
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "a default parameter with an effect",
    source: `
const o = { n: 0 };
function f(x = (o.n = 1)) { return x; }
f();
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "a computed key with an effect",
    source: `
const o = { n: 0 };
const t = { [(o.n = 1, "k")]: 1 };
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 2, 1, 3], behavior: "differs" }],
  },
  {
    name: "an optional call",
    source: `
const o = { n: 0 };
const f = () => { o.n = 1; };
f?.();
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "a tagged template",
    source: `
const o = { n: 0 };
const tag = (s) => { o.n = 1; return s; };
const t = tag\`x\`;
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "a bound function called later",
    source: `
const o = { n: 0 };
const f = function () { o.n = this.k; };
const b = f.bind({ k: 1 });
b();
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 2, 4, 3, 5], behavior: "differs" }],
  },
  {
    name: "Reflect.apply",
    source: `
const o = { n: 0 };
const f = () => { o.n = 1; };
Reflect.apply(f, null, []);
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "a function made by new Function",
    source: `
const o = { n: 0 };
globalThis.__o = o;
const f = new Function("__o.n = 1");
f();
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 2, 3, 5, 4, 6], behavior: "differs" }],
  },
  {
    name: "sort comparator",
    source: `
const o = { n: 0 };
const a = [2, 1];
a.sort((x, y) => { o.n = 1; return x - y; });
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 1, 3, 2, 4], behavior: "differs" }],
  },
  {
    name: "replace callback",
    source: `
const o = { n: 0 };
const s = "abc".replace("b", () => { o.n = 1; return "x"; });
const w = o.n === 1 ? "one" : "zero";
console.log(w);
`,
    reorders: [{ order: [0, 2, 1, 3], behavior: "differs" }],
  },
  {
    name: "vendor premise: listed functions and their members allocate, others are the world",
    source: `
import { make, kit, other } from "./v.js";
const s = make("x");
const t = s.extend({ y: 1 }).optional();
const k = kit();
other();
const u = s.parse(1);
`,
    expect: ["pure", "pure", "pure", "world", "world"],
    imports: [
      { from: "./v.js", local: "make", imported: "make" },
      { from: "./v.js", local: "kit", imported: "kit" },
      { from: "./v.js", local: "other", imported: "other" },
    ],
  },
];
