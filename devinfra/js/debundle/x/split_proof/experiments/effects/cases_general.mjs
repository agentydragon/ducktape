// Minimal programs for the general rules of the effect analysis. Each was accepted in an order Node runs differently by an
// earlier version of the rules (found by a fresh agent that tried to break them); the programs that still are accepted
// are in `debug/general_known_gaps/`. One unit per line; `order` lists the unit indexes of the reordered program, which
// Node prints differently from the original and `translate` must not accept. Premises are `{}`: the world may add
// accessors and conversion hooks to anything (P3 off). The cases made with `p3` set premise P3 (`noWorldHooks`), as the
// fuzzers do: the generators of `../translation/gen` and the footprint oracle found them, and with P3 off a read of an
// object the world holds is a call into the world, which hides some of them.
export const premises = {};

const c = (name, source, order) => ({ name, source: source.trim() + "\n", reorders: [{ order, behavior: "differs" }] });
const p3 = (name, source, order) => ({ ...c(name, source, order), premises: { noWorldHooks: true } });
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
  c(
    "String.raw reads a getter two levels into its argument",
    `
const o = { n: 0 };
String.raw({ raw: { get length() { o.n = 1; return 0; } } });
console.log(o.n);
`,
    [0, 2, 1]
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
  // a call is run again unless the heap and the throws it left are known: the call memo replays only a run that grew nothing
  c(
    "memo: a replayed call drops the value it throws",
    `
const shared = { n: 0 };
function f(b) { if (b) throw shared; }
f(false);
try { f(true); } catch (e) { e.n = 1; }
console.log(shared.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "memo: a second identical call is replayed, not run, and would move the value one hop further",
    `
const o = { n: 0 };
const s = { p: undefined, q: undefined, adv() { this.q = this.p; this.p = o; } };
s.adv();
s.adv();
s.q.n = 1;
console.log(o.n);
`,
    [0, 1, 2, 3, 5, 4]
  ),
  c(
    "memo: a loop ends after one pass because the replayed call changes nothing",
    `
const o = { n: 0 };
const s = { p: undefined, q: undefined, adv() { this.q = this.p; this.p = o; } };
for (let i = 0; i < 2; i++) s.adv();
s.q.n = 1;
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  // a named read of an array or a collection finds what was stored without a name: under a computed key, by `defineProperty`
  c(
    "array: a value stored under a computed key is not found by a named read",
    `
const o = { n: 0 };
const arr = [];
arr["fo" + "o"] = o;
arr.foo.n = 1;
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "array: a function stored under a computed key is not called by a named call",
    `
const o = { n: 0 };
const arr = [];
arr["fo" + "o"] = () => { o.n = 1; };
arr.foo();
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "Map instance: a value stored under a computed key, read by name",
    `
const o = { n: 0 };
const m = new Map();
m["fo" + "o"] = o;
m.foo.n = 1;
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "array: Object.defineProperty value, read by name",
    `
const o = { n: 0 };
const arr = [];
Object.defineProperty(arr, "x", { value: o });
arr.x.n = 1;
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "array: a numeric literal key that is no index, read as a string",
    `
const o = { n: 0 };
const arr = [];
arr[-1] = o;
arr["-1"].n = 1;
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  // hooks of arrays and collections: installed by `Object.assign` or found on a replaced prototype
  c(
    "array: an own Symbol.iterator installed by Object.assign, then a spread",
    `
const o = { n: 0 };
const arr = [1];
Object.assign(arr, { [Symbol.iterator]() { o.n = 1; return [][Symbol.iterator](); } });
[...arr];
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "array: a toString found on a replaced prototype, converted by a template",
    `
const o = { n: 0 };
const arr = [1];
Object.setPrototypeOf(arr, { toString() { o.n = 1; return "x"; } });
\`\${arr}\`;
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "Map instance: a Symbol.iterator found on a replaced prototype, then a spread",
    `
const o = { n: 0 };
const m = new Map([[1, 2]]);
Object.setPrototypeOf(m, { [Symbol.iterator]() { o.n = 1; return [][Symbol.iterator](); } });
[...m];
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  // the fields of a derived class are initialized once `super()` has returned
  c(
    "derived field initializer reads a field that the parent set",
    `
const o = { n: 0 };
class P { y = o; }
class D extends P { x = this.y; }
new D().x.n = 1;
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  // static fields and static blocks run after every method of the class is defined
  c(
    "static field calls a static method that is defined later in the body",
    `
const o = { n: 0 };
class A { static x = A.m(); static m() { o.n = 1; } }
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "static block calls a static method that is defined later in the body",
    `
const o = { n: 0 };
class A { static { A.m(); } static m() { o.n = 1; } }
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "static field calls this.m() for a method that is defined later",
    `
const o = { n: 0 };
class A { static x = this.m(); static m() { o.n = 1; } }
console.log(o.n);
`,
    [0, 2, 1]
  ),
  // a parent constructor that returns an object makes it `this`
  c(
    "a parent constructor returns an object; the derived class has no constructor",
    `
const o = { n: 0 };
class P { constructor() { return o; } }
class D extends P {}
new D().n = 1;
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "a derived constructor writes this after super() returned an object",
    `
const o = { n: 0 };
class P { constructor() { return o; } }
class D extends P { constructor() { super(); this.n = 1; } }
new D();
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "the parent is a function that returns an object",
    `
const o = { n: 0 };
function Base() { return o; }
class D extends Base {}
new D().n = 1;
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  // parameter defaults do not see what the body declares
  c(
    "a parameter default names an outer variable that the body redeclares",
    `
const o = { n: 0 };
function f(a = o) { var o; a.n = 1; }
f();
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "a parameter default calls an outer function that the body redeclares",
    `
const o = { n: 0 };
function g() { return o; }
function f(a = g()) { function g() { return {}; } a.n = 1; }
f();
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  // function declarations in a `switch` case
  c(
    "a function declaration in a switch case is never bound",
    `
const o = { n: 0 };
switch (1) { case 1: g(); function g() { o.n = 1; } }
console.log(o.n);
`,
    [0, 2, 1]
  ),
  c(
    "a function declared in a switch case, stored and called later",
    `
const o = { n: 0 };
const reg = {};
switch (1) { case 1: reg.f = g; function g() { o.n = 1; } }
reg.f();
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  // `super(…)` into a standard constructor builds the instance
  c(
    "class extends Map: the entries given to super are not held by the instance",
    `
const o = { n: 0 };
class M extends Map {}
new M([["k", o]]).get("k").n = 1;
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  c(
    "class extends Set: the items given to super are not held by the instance",
    `
const o = { n: 0 };
class S extends Set {}
new S([o]).forEach((x) => { x.n = 1; });
console.log(o.n);
`,
    [0, 1, 3, 2]
  ),
  // closures made at one site with different `this`, bound targets or arguments
  c(
    "bound functions of one site: the second bind replaces the first",
    `
const o = { n: 0 };
function setN() { this.n = 1; }
const bs = [setN.bind(o), setN.bind({})];
bs.shift()();
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "bound functions of one site with different arguments",
    `
const o = { n: 0 };
function setN(t) { t.n = 1; }
const bs = [setN.bind(null, o), setN.bind(null, {})];
bs.shift()();
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  c(
    "arrows of one site made with different this: the second replaces the first",
    `
const o = { n: 0 };
function mk() { return () => { this.n = 1; }; }
const fs = [mk.call(o), mk.call({})];
fs.shift()();
console.log(o.n);
`,
    [0, 1, 2, 4, 3]
  ),
  // a standard function used as a conversion hook or an accessor does what its row says
  c(
    "valueOf is a standard function that mutates its receiver",
    `
const t = { 0: "a", length: 1, valueOf: Array.prototype.shift };
t * 1;
console.log(t.length);
`,
    [0, 2, 1]
  ),
  c(
    "a getter is a standard function that mutates its receiver",
    `
const t = { 0: "a", length: 1 };
Object.defineProperty(t, "x", { get: Array.prototype.pop });
t.x;
console.log(t.length);
`,
    [0, 1, 3, 2]
  ),
  // a member access inside an optional chain or parentheses: `delete`, a call, a tag and the patches read off code that is not run
  p3(
    "delete n?.[b] deletes a property of n",
    `
let n = true ? { a: 1, b: "s" } : null;
delete n?.["b"];
const seen = n.b;
console.log(seen);
`,
    [0, 2, 1, 3]
  ),
  p3(
    "delete n?.inner?.x deletes a property of n.inner",
    `
let n = true ? { inner: { x: 1 } } : null;
delete n?.inner?.x;
const seen = n.inner.x;
console.log(seen);
`,
    [0, 2, 1, 3]
  ),
  p3(
    "delete n?.b inside a function that is called",
    `
const n = { b: "s" };
function drop() { delete n?.b; }
drop();
const seen = n.b;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "delete n?.b inside a static getter that is read",
    `
const n = { b: "s" };
class C { static get g() { delete n?.b; return 1; } }
const x = C.g;
const seen = n.b;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "a tag through an optional chain is called with its object as this",
    `
const n = { c: 0, tag() { this.c = 1; } };
(n?.tag)\`\`;
const seen = n.c;
console.log(seen);
`,
    [0, 2, 1, 3]
  ),
  p3(
    "the computed key of a tag is evaluated",
    `
const o = { c: 0 };
const n = { tag() {} };
n[(o.c = 1, "tag")]\`\`;
const seen = o.c;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "the computed key of a callee is converted to a key",
    `
const o = { c: 0 };
const key = { toString() { o.c = 1; return "m"; } };
const n = { m() {} };
n[key]();
const seen = o.c;
console.log(seen);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  p3(
    "delete Math?.max patches an intrinsic",
    `
delete Math?.max;
const kind = typeof Math.max;
console.log(kind);
`,
    [1, 0, 2]
  ),
  p3(
    "delete Math?.max in a generator body that is not run here",
    `
const it = (function* () { delete Math?.max; yield 1; })();
it.next();
const kind = typeof Math.max;
console.log(kind);
`,
    [0, 2, 1, 3]
  ),
  p3(
    "Reflect?.deleteProperty of an intrinsic in a generator body",
    `
const it = (function* () { (Reflect?.deleteProperty)(Math, "max"); yield 1; })();
it.next();
const kind = typeof Math.max;
console.log(kind);
`,
    [0, 2, 1, 3]
  ),
  // what a standard function returns is an object of the right kind: `Array(n)` an array, `new String(s)` an object, and `JSON.parse` anything
  p3(
    "forEach of new Array(1).fill(7) runs a callback that writes o",
    `
const o = { n: 0 };
const a = new Array(1).fill(7);
a.forEach(() => { o.n++; });
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "Array(2).fill(o) stores o, and a write through the array writes o",
    `
const o = { n: 0 };
const a = Array(2).fill(o);
a[0].n = 1;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "Array.apply(null, Array(2)) is an array: map runs its callback",
    `
const o = { n: 0 };
Array.apply(null, Array(2)).map(() => { o.n++; });
const seen = o.n;
console.log(seen);
`,
    [0, 2, 1, 3]
  ),
  p3(
    "for-of over Array(2).fill(o) yields o",
    `
const o = { n: 0 };
for (const x of Array(2).fill(o)) x.n = 1;
const seen = o.n;
console.log(seen);
`,
    [0, 2, 1, 3]
  ),
  p3(
    "JSON.parse of an array is an array: forEach runs its callback",
    `
const o = { n: 0 };
const parsed = JSON.parse("[1, 2]");
parsed.forEach(() => { o.n++; });
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "JSON.parse holds what its reviver returned, at any depth",
    `
const o = { n: 0 };
const parsed = JSON.parse('{"a":{"b":1}}', (k, v) => (k === "b" ? o : v));
parsed.a.b.n = 1;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "the reviver of JSON.parse gets what it returned for a member",
    `
const o = { n: 0 };
JSON.parse('{"a":{"b":1}}', (k, v) => { if (k === "b") return o; if (k === "a") v.b.n = 1; return v; });
const seen = o.n;
console.log(seen);
`,
    [0, 2, 1, 3]
  ),
  p3(
    "new String is an object that holds what is stored in it",
    `
const o = { n: 0 };
const s = new String("a");
s.held = o;
s.held.n = 1;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  p3(
    "new Number is an object: a method stored in it is called",
    `
const o = { n: 0 };
const boxed = new Number(1);
boxed.bump = () => { o.n++; };
boxed.bump();
const seen = o.n;
console.log(seen);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  p3(
    "Object(1) is an object: a method stored in it is called",
    `
const o = { n: 0 };
const boxed = Object(1);
boxed.bump = () => { o.n++; };
boxed.bump();
const seen = o.n;
console.log(seen);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  // iterators: the helpers of the iterator of a collection run their callbacks, at once or when the result is consumed, and the
  // entries of a collection are pairs
  p3(
    "toArray of an iterator helper runs the callback of map",
    `
const o = { n: 0 };
const mapped = new Map([["k", 1]]).values().map((x) => { o.n++; return x; });
mapped.toArray();
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "next of an iterator helper runs the callback of map",
    `
const o = { n: 0 };
const mapped = new Map([["k", 1]]).values().map((x) => { o.n++; return x; });
mapped.next();
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "a spread of an iterator helper runs the callback of map",
    `
const o = { n: 0 };
const mapped = new Set([1]).values().map((x) => { o.n++; return x; });
[...mapped];
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "for-of over an iterator helper runs the callback of filter",
    `
const o = { n: 0 };
const kept = new Set([1]).values().filter((x) => { o.n++; return true; });
for (const x of kept) {}
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "some of a Set iterator runs its callback at once",
    `
const o = { n: 0 };
new Set([1]).values().some(() => { o.n++; return true; });
const seen = o.n;
console.log(seen);
`,
    [0, 2, 1, 3]
  ),
  p3(
    "reduce of a Map iterator runs its callback at once",
    `
const o = { n: 0 };
new Map([["k", 1]]).keys().reduce((a) => { o.n++; return a; }, 0);
const seen = o.n;
console.log(seen);
`,
    [0, 2, 1, 3]
  ),
  p3(
    "a helper of the iterator of matchAll runs its callback when it is consumed",
    `
const o = { n: 0 };
const mapped = "ab".matchAll(/a/g).map((x) => { o.n++; return x; });
[...mapped];
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "next of the entries of a Map yields a pair that holds the value",
    `
const o = { n: 0 };
const m = new Map([["k", o]]);
m.entries().next().value[1].n = 1;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "a spread of a Map yields pairs that hold the values",
    `
const o = { n: 0 };
const m = new Map([["k", o]]);
[...m][0][1].n = 1;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "Array.from of a Map yields pairs that hold the values",
    `
const o = { n: 0 };
const m = new Map([["k", o]]);
Array.from(m)[0][1].n = 1;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "the entries of a Set are pairs that hold the element",
    `
const o = { n: 0 };
const s = new Set([o]);
Array.from(s.entries())[0][1].n = 1;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "for-of over a Map with a plain loop variable",
    `
const o = { n: 0 };
const m = new Map([["k", o]]);
for (const pair of m) pair[1].n = 1;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  p3(
    "a Set made from a Map holds pairs",
    `
const o = { n: 0 };
const m = new Map([["k", o]]);
const s = new Set(m);
s.forEach((pair) => { pair[1].n = 1; });
const seen = o.n;
console.log(seen);
`,
    [0, 1, 2, 4, 3, 5]
  ),
  p3(
    "the map function of Array.from gets the pairs of a Map",
    `
const o = { n: 0 };
const m = new Map([["k", o]]);
Array.from(m, (pair) => { pair[1].n = 1; });
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
  // limits of the analysis that are passed: a prototype chain longer than is followed, arrays nested deeper than `flat` looked
  p3(
    "a getter 11 prototypes up writes o",
    `
const o = { n: 0 };
const p0 = { get g() { o.n = 1; return 1; } };
${chain(11)}
p11.g;
console.log(o.n);
`,
    [...Array(13).keys(), 14, 13]
  ),
  p3(
    "a toString 12 prototypes up runs in a template literal",
    `
const o = { n: 0 };
const p0 = { toString() { o.n = 1; return "s"; } };
${chain(12)}
\`\${p12}\`;
console.log(o.n);
`,
    [...Array(14).keys(), 15, 14]
  ),
  p3(
    "a valueOf 12 prototypes up runs in an addition",
    `
const o = { n: 0 };
const p0 = { valueOf() { o.n = 1; return 1; } };
${chain(12)}
p12 + 1;
console.log(o.n);
`,
    [...Array(14).keys(), 15, 14]
  ),
  p3(
    "flat(Infinity) reaches an element seven arrays deep",
    `
const o = { n: 0 };
const flat = [[[[[[[o], 1], 2, 2], 3, 3, 3], 4, 4, 4, 4], 5, 5, 5, 5, 5], 6, 6, 6, 6, 6, 6].flat(Infinity);
flat[0].n = 1;
const seen = o.n;
console.log(seen);
`,
    [0, 1, 3, 2, 4]
  ),
];
