// Minimal programs for the general rules of the effect analysis. Each was accepted in an order Node runs differently by an
// earlier version of the rules (found by a fresh agent that tried to break them); the programs that still are accepted
// are in `debug/general_known_gaps/`. One unit per line; `order` lists the unit indexes of the reordered program, which
// Node prints differently from the original and `translate` must not accept. Premises are `{}`: the world may add
// accessors and conversion hooks to anything (P3 off).
export const premises = {};

const c = (name, source, order) => ({ name, source: source.trim() + "\n", reorders: [{ order, behavior: "differs" }] });
const chain = (n) => Array.from({ length: n }, (_, i) => `const p${i + 1} = Object.create(p${i});`).join("\n");

export const cases = [
  // callbacks of standard functions: which parameter holds what, and `thisArg`
  c(
    "reduce: the element is the 2nd callback parameter",
    `
const o = { n: 0 };
[o].reduce((a, x) => { x.n = 1; return a; }, 0);
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "reduce: the initial accumulator is the 1st parameter",
    `
const o = { n: 0 };
[1].reduce((a, x) => { a.n = 1; return a; }, o);
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "sort: comparator 2nd parameter",
    `
const o = { n: 0 };
[o, o].sort((a, b) => { b.n = 1; return 0; });
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "thisArg of forEach is ignored",
    `
const o = { n: 0 };
[1].forEach(function () { this.n = 1; }, o);
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "Map.forEach: the key is the 2nd parameter",
    `
const k = { n: 0 };
new Map([[k, 1]]).forEach((v, key) => { key.n = 1; });
console.log(k.n);
`,
    [0, 2, 1]
  ),
  c(
    "Set.forEach: the value is also the 2nd parameter",
    `
const k = { n: 0 };
new Set([k]).forEach((v, v2) => { v2.n = 1; });
console.log(k.n);
`,
    [0, 2, 1]
  ),
  c(
    "a reduce callback reads what the previous call returned",
    `
const o = { n: 0 };
[1, 2].reduce((a, x) => { if (a.deep) a.deep.n = 1; return { deep: o }; }, {});
console.log(o.n);
`,
    [0, 2, 1]
  ),
  // what a standard function returns holds what the real one holds
  c(
    "reduce returns the initial value",
    `
const o = { n: 0 };
const r = [].reduce(() => ({}), o);
r.n = 1;
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "Array.from drops the mapFn results",
    `
const o = { n: 0 };
Array.from([1], () => o).at(0).n = 1;
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "JSON.parse returns the reviver's result",
    `
const o = { n: 0 };
JSON.parse("1", () => o).n = 1;
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "Array.of holds the arguments",
    `
const o = { n: 0 };
Array.of(o).at(0).n = 1;
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "concat holds the argument's elements",
    `
const o = { n: 0 };
[].concat([o]).at(0).n = 1;
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "flat holds the nested elements",
    `
const o = { n: 0 };
[[o], 2].flat().at(0).n = 1;
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "flatMap holds the returned arrays' elements",
    `
const o = { n: 0 };
[1].flatMap(() => [o]).at(0).n = 1;
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "Object.entries entries are [key, value] pairs",
    `
const o = { n: 0 };
Object.entries({ k: o }).at(0).at(1).n = 1;
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "new Map(map): the copy does not hold the values (contentsOf twice)",
    `
const o = { n: 0 };
const m = new Map([["k", o]]);
new Map(m).get("k").n = 1;
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "Object.fromEntries(map)",
    `
const o = { n: 0 };
const m = new Map([["k", o]]);
Object.fromEntries(m).k.n = 1;
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  // descriptor maps of `Object.create` and `Object.defineProperties`
  c(
    "Object.defineProperties accessor",
    `
const o = { n: 0 };
Object.defineProperties({}, { v: { get() { o.n = 1; return 1; } } }).v;
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "Object.create(null, descriptors) accessor",
    `
const o = { n: 0 };
Object.create(null, { v: { get() { o.n = 1; return 1; } } }).v;
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "Object.create(null, descriptors) method",
    `
const o = { n: 0 };
Object.create(null, { f: { value() { o.n = 1; } } }).f();
console.log(o.n);
`,
    [0, 2, 1]
  ),
  // the iterator of a collection: a live view with a cursor, and its helpers
  c(
    "two next() calls on a Map iterator",
    `
const m = new Map([["a", 1], ["b", 2]]);
const it = m.keys();
const x = it.next().value;
const y = [it.next().value];
console.log(x, y);
`,
    [0, 1, 3, 2, 4]
  ),
  c(
    "the iterator is a live view of the Map",
    `
const m = new Map();
const it = m.values();
const o = { n: 0 };
m.set("k", o);
const [first] = [...it];
first.n = 1;
console.log(o.n);
`,
    [0, 1, 2, 3, 4, 6, 5]
  ),
  c(
    "iterator helper map runs its callback when consumed",
    `
const o = { n: 0 };
const m = new Map([["a", 1]]);
const mapped = m.values().map((x) => { o.n++; return x; });
const arr = mapped.toArray();
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "keys().next().value is the key object",
    `
const k = { evicted: false };
const cache = new Map([[k, 1]]);
const oldest = cache.keys().next().value;
oldest.evicted = true;
console.log(k.evicted);
`,
    [0, 1, 2, 4, 3]
  ),
  // spreads: positions after one, `apply`, `new`, and `Object.assign` with many sources
  c(
    "forwarding wrapper (...args) => f(...args)",
    `
const o = { n: 0 };
const f = (a, b) => { b.n = 1; };
const g = (...args) => f(...args);
g(1, o);
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "an argument after a spread shifts left",
    `
const o = { n: 0 };
const f = (a, b, c) => { c.n = 1; };
f(...[1, 2], o);
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "apply with five parameters",
    `
const o = { n: 0 };
const f = (a, b, c, d, e) => { e.n = 1; };
f.apply(null, [1, 2, 3, 4, o]);
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "new C(...args)",
    `
const o = { n: 0 };
class C { constructor(a, b) { b.n = 1; } }
new C(...[1, o]);
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "Object.assign has copy positions for four sources only",
    `
const o = { n: 0 };
Object.assign({}, {}, {}, {}, {}, { get x() { o.n = 1; return 1; } });
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "a parameter past the positions a spread fills",
    `
const o = { n: 0 };
const f = (a, b, c, d, e, g, h, i, j, k, l, m, n, p, q, r, s) => { s.n = 1; };
f(...[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, o]);
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  // accessors whose key is computed
  c(
    "getter with a computed key",
    `
const o = { n: 0 };
const key = "v";
const g = { get [key]() { o.n = 1; return 1; } };
g.v;
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "setter with a computed key",
    `
const o = { n: 0 };
const key = "v";
const g = { set [key](x) { o.n = x; } };
g.v = 1;
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "class static getter with a computed key",
    `
const o = { n: 0 };
const key = "s";
class K { static get [key]() { o.n = 1; return 1; } }
K.s;
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "getter with a numeric literal key read as a string",
    `
const o = { n: 0 };
const g = { get 1() { o.n = 1; return 1; } };
g["1"];
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  // a class constructor that returns another object
  c(
    "class constructor returns another object",
    `
const shared = { n: 0 };
class K { constructor() { return shared; } }
new K().n = 1;
console.log(shared.n);
`,
    [0, 1, 3, 2]
  ),
  // where the language converts a key or an element to a string
  c(
    "o[key] = v converts key",
    `
const log = { n: 0 };
const key = { toString() { log.n = 1; return "k"; } };
const t = {};
t[key] = 1;
console.log(log.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "{ [key]: v } converts key",
    `
const log = { n: 0 };
const key = { toString() { log.n = 1; return "k"; } };
const t = { [key]: 1 };
console.log(log.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "delete o[key] converts key",
    `
const log = { n: 0 };
const key = { toString() { log.n = 1; return "k"; } };
const t = { k: 1 };
delete t[key];
console.log(log.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "class { [key]() {} } converts key",
    `
const log = { n: 0 };
const key = { toString() { log.n = 1; return "k"; } };
class K { [key]() {} }
console.log(log.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "const { [key]: x } = o converts key",
    `
const log = { n: 0 };
const key = { toString() { log.n = 1; return "k"; } };
const { [key]: x } = { k: 1 };
console.log(log.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "sort() without comparator converts the elements",
    `
const log = { n: 0 };
const a = { toString() { log.n = 1; return "a"; } };
const arr = [a, { toString() { return "b"; } }];
arr.sort();
console.log(log.n);
`,
    [0, 1, 2, 4, 3]
  ),
  // an async function that returns a thenable: its `then` runs when the promise is resolved
  c(
    "async function returns a thenable",
    `
const log = [];
const t1 = { then(r) { log.push("t1"); r(1); } };
const t2 = { then(r) { log.push("t2"); r(2); } };
async function a() { return t1; }
async function b() { return t2; }
a();
b();
Promise.resolve().then(() => console.log(log.join()));
`,
    [0, 1, 2, 3, 4, 6, 5, 7]
  ),
  // `x.constructor` of an instance of a class or a function of the program
  c(
    "inst.constructor reads class statics",
    `
let inst;
class C {}
inst = new C();
C.x = 5;
const r = inst.constructor.x;
console.log(r);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  c(
    "inst.constructor reads the properties of a function",
    `
let inst;
function F() {}
inst = new F();
F.x = 5;
const r = inst.constructor.x;
console.log(r);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  // a prototype chain of 11 objects: nothing may be lost at the analysis's depth limit
  c(
    "method 11 prototypes up",
    `
const o = { n: 0 };
const p0 = { m() { o.n = 1; } };
${chain(11)}
p11.m();
console.log(o.n);
`,
    [...Array(13).keys(), 14, 13]
  ),
  c(
    "getter 11 prototypes up",
    `
const o = { n: 0 };
const p0 = { get g() { o.n = 1; return 1; } };
${chain(11)}
p11.g;
console.log(o.n);
`,
    [...Array(13).keys(), 14, 13]
  ),
  // a standard function calls a method of its receiver or argument through a protocol
  c(
    "RegExp.prototype.test calls an own exec",
    `
const o = { n: 0 };
const re = /x/;
re.exec = function () { o.n = 1; return null; };
re.test("abc");
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "String(array) calls an own join",
    `
const o = { n: 0 };
const arr = [1];
arr.join = function () { o.n = 1; return "x"; };
String(arr);
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "spreading an array with an own Symbol.iterator",
    `
const o = { n: 0 };
const arr = [1, 2];
arr[Symbol.iterator] = function () { o.n = 1; return [][Symbol.iterator](); };
[...arr];
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "Symbol.replace receives the replacement",
    `
const o = { n: 0 };
const sep = { [Symbol.replace](s, r) { r.n = 1; return "x"; } };
"abc".replace(sep, o);
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "Symbol.hasInstance receives the operand",
    `
const o = { n: 0 };
class Odd { static [Symbol.hasInstance](x) { x.n = 1; return true; } }
o instanceof Odd;
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "Object.prototype.toLocaleString calls toString",
    `
const o = { n: 0 };
const x = { toString() { o.n = 1; return "x"; } };
x.toLocaleString();
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "Error.prototype.toString reads name",
    `
const o = { n: 0 };
const x = { get name() { o.n = 1; return "N"; } };
Error.prototype.toString.call(x);
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "new Error(m, options) reads the cause getter",
    `
const o = { n: 0 };
new Error("m", { get cause() { o.n = 1; return 1; } });
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "Intl.NumberFormat reads option getters",
    `
const o = { n: 0 };
new Intl.NumberFormat("en", { get style() { o.n = 1; return "decimal"; } });
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "a Map with an own toString in a template literal",
    `
const o = { n: 0 };
const m = new Map();
m.toString = () => { o.n = 1; return "m"; };
\`\${m}\`;
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  // a pattern with the sticky or global flag writes its `lastIndex`
  c(
    "sticky match writes lastIndex",
    `
const re = /a/y;
"aa".match(re);
console.log(re.lastIndex);
`,
    [0, 2, 1]
  ),
  c(
    "global replace resets lastIndex",
    `
const re = /a/g;
re.lastIndex = 3;
"aaa".replace(re, "b");
console.log(re.lastIndex);
`,
    [0, 1, 3, 2]
  ),
  // `arguments` inside an arrow function is the enclosing function's
  {
    // precision: `arguments` is a binding of the function, so two calls that only read it commute
    name: "two calls that read their own arguments commute",
    source: `
const a = { n: 0 };
const b = { n: 0 };
const first = function () { return arguments[0]; };
const x = first(a);
const y = first(b);
console.log(x === a, y === b);
`.trimStart(),
    reorders: [{ order: [0, 1, 2, 4, 3, 5], behavior: "same", accepted: true }],
  },
  c(
    "arguments inside an arrow function",
    `
const o = { n: 0 };
function outer() { return (() => { const [first] = arguments; return first; })(); }
outer(o).n = 1;
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "arrow forwards the enclosing function's arguments",
    `
const o = { n: 0 };
function target(a) { a.n = 1; }
function wrapper() { return (() => target.apply(null, arguments))(); }
wrapper(o);
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
];
