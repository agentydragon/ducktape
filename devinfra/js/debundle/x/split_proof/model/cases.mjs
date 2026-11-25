// Programs for `differential.mjs`, for what the generator rarely writes. Each runs in Node and in the model and must
// print the same and end the same way. A case with `end: "unknown"` uses a value of the host that the model has no
// rule for (`allowHost`) or an operation the model leaves to the host. `expectedStop` also checks the reason,
// how much of the recording was consumed, and the output prefix.
export const inheritanceCases = [
  {
    name: "default derived constructors forward arguments and the final instance prototype",
    source: `
      class Base {
        field = 4;
        constructor(a, b) { this.value = a + b; }
        read() { return this.value + this.field; }
        static label() { return this.name; }
      }
      class Middle extends Base {}
      class Leaf extends Middle { readLeaf() { return this.read(); } }
      const leaf = new Leaf(2, 3);
      console.log(leaf.readLeaf(), leaf.constructor === Leaf, Leaf.length, Leaf.label());
      console.log(Object.getPrototypeOf(Leaf) === Middle,
        Object.getPrototypeOf(Leaf.prototype) === Middle.prototype,
        Object.getPrototypeOf(leaf) === Leaf.prototype);
      try { Leaf(); } catch (e) { console.log("class call rejected"); }
      try { new leaf.readLeaf(); } catch (e) { console.log("method new rejected"); }
    `,
  },
  {
    name: "default derived construction consults the current superclass without changing the instance prototype",
    source: `
      class Base { constructor(x) { this.value = x; } }
      class Alternate { constructor(x) { this.value = x + 10; } }
      class Derived extends Base {}
      Object.setPrototypeOf(Derived, Alternate);
      const value = new Derived(3);
      console.log(value.value, Object.getPrototypeOf(value) === Derived.prototype,
        Object.getPrototypeOf(Derived.prototype) === Base.prototype);
      Object.setPrototypeOf(Derived, {});
      try { new Derived(); } catch (e) { console.log("nonconstructor rejected"); }
    `,
  },
  {
    name: "default derived construction preserves base object returns and thrown completions",
    source: `
      class Base { constructor(x) { if (x < 0) throw x; if (x) return { replacement: x }; return 9; } }
      class Derived extends Base {}
      console.log(new Derived(2).replacement, Object.getPrototypeOf(new Derived(0)) === Derived.prototype);
      try { new Derived(-1); } catch (e) { console.log(e); }
    `,
  },
  {
    name: "default derived construction initializes each class's own fields after its superclass",
    source: `
      let key = "items";
      const seed = 5;
      class Base {
        constructor(value) {
          this.value = value;
          this.before = this.items;
        }
        set ready(value) { throw value; }
      }
      class Middle extends Base {
        [key] = [this.value, seed];
        ready = this.items[0] + this.items[1];
      }
      key = "renamed";
      class Leaf extends Middle { total = this.ready + 1; }
      const first = new Leaf(2);
      const second = new Leaf(3);
      console.log(first.before === undefined, first.items !== second.items, first.total, second.total,
        "renamed" in first, Object.getPrototypeOf(first) === Leaf.prototype);
    `,
    end: "normal",
  },
  {
    name: "derived fields initialize on a replacement object but skip a throwing superclass",
    source: `
      let runs = 0;
      const replacement = {};
      class Base {
        constructor(value) { if (value < 0) throw value; return replacement; }
      }
      class Derived extends Base { marker = ++runs; }
      try { new Derived(-1); } catch (error) { console.log(error, runs, "marker" in replacement); }
      const instance = new Derived(1);
      console.log(instance === replacement, instance.marker, runs);
    `,
    end: "normal",
  },
  {
    name: "null heritage and null superclass prototype have distinct construction behavior",
    source: `
      class Empty extends null {}
      console.log(Object.getPrototypeOf(Empty.prototype), Empty.length);
      try { new Empty(); } catch (e) { console.log("null base rejected"); }
      function Base() { this.value = 7; }
      Base.prototype = null;
      class Derived extends Base {}
      console.log(Object.getPrototypeOf(Derived.prototype), new Derived().value,
        Object.getPrototypeOf(new Derived()) === Derived.prototype);
    `,
  },
  {
    name: "heritage evaluation reads the prototype once and rejects invalid superclass values",
    source: `
      let count = 0;
      function choose() { count++; return Base; }
      function Base() {}
      Base.prototype = { value: 4 };
      class Derived extends choose() {}
      Base.prototype = { value: 9 };
      console.log(count, new Derived().value);
      Base.prototype = 1;
      try { const Bad = class extends Base {}; } catch (e) { console.log("bad prototype"); }
      try { const Bad = class extends 1 {}; } catch (e) { console.log("bad base"); }
      const arrow = () => 1;
      Object.defineProperty(arrow, "prototype", { get() { count++; return {}; } });
      try { const Bad = class extends arrow {}; } catch (e) { console.log("arrow rejected", count); }
    `,
  },
  {
    name: "derived class name is uninitialized in heritage and captured by methods",
    source: `
      class Base {}
      const Derived = class Inner extends Base { self() { return Inner; } };
      console.log(new Derived().self() === Derived);
      try { const Invalid = class Inner extends Inner {}; } catch (e) { console.log("self TDZ"); }
      let Later;
      try { const Invalid = class extends Later {}; } catch (e) { console.log("undefined base"); }
    `,
  },
];

const heritageSplit = {
  entry: "entry.mjs",
  modules: { "prefix.mjs": ["#0", "#1", "#3", "#4"], "entry.mjs": ["#2", "#5"] },
};

export const inheritanceSplitCases = [
  {
    name: "reassigning the superclass breaks the heritage prefix",
    source: `
      var Base = class { value() { return 1; } };
      Base = class { value() { return 2; } };
      var Earlier = class extends Base {};
      var Pivot = class extends Base {};
      var config = { factor: 3 };
      var local = [5];
      console.log(new Earlier().value(), new Pivot().value(), config.factor, local[0]);
    `,
    spec: {
      entry: "entry.mjs",
      modules: { "prefix.mjs": ["#0", "#1", "#2", "#4", "#5"], "entry.mjs": ["#3", "#6"] },
    },
    uncertifiedHeritage: true,
  },
  {
    name: "a function superclass lacks the recognized class origin",
    source: `
      var Base = function (value) { this.value = value; };
      var Earlier = class extends Base {};
      var Pivot = class extends Base {};
      var config = { factor: 3 };
      var local = [5];
      console.log(new Earlier(7).value, new Pivot(11).value, config.factor, local[0]);
    `,
    spec: heritageSplit,
    uncertifiedHeritage: true,
  },
  {
    name: "a declaration cannot read the subclass before its initialization",
    source: `
      var Base = class {};
      var Earlier = class extends Base {};
      var Pivot = class extends Base {};
      var saved = Pivot;
      var local = [5];
      console.log(Object.getPrototypeOf(Earlier) === Base, saved === Pivot, local[0]);
    `,
    spec: heritageSplit,
    refused: true,
  },
  {
    name: "heritage checks replay after another subclass and across local allocations",
    source: `
      var Base = class {
        constructor(value) { this.value = value; }
        get amount() { return this.value; }
      };
      var Earlier = class extends Base { read() { return this.amount + 1; } };
      var Pivot = class extends Base { read() { return this.amount * 2; } };
      var config = { factor: 3 };
      var local = [5];
      console.log(new Earlier(7).read(), new Pivot(11).read(), config.factor, local[0],
        Object.getPrototypeOf(Pivot) === Base, Object.getPrototypeOf(Pivot.prototype) === Base.prototype);
    `,
    spec: heritageSplit,
    certifiedHeritage: { pivot: 2, crossed: [3, 4] },
  },
];

// Each coefficient · 2^exponent is an exact binary64 rounding boundary. Use
// integer arithmetic so neither decimal neighbor rounds before the test runs.
function roundingBoundaryStrings() {
  const midpoints = [
    [1n, -1075], // zero and the least subnormal
    [3n, -1075], // adjacent subnormals, with the upper significand even
    [(1n << 53n) - 1n, -1075], // largest subnormal and least normal
    [(1n << 53n) + 1n, -53], // just above 1, with the lower significand even
    [(1n << 53n) + 3n, -53], // just above 1, with the upper significand even
    [(1n << 54n) - 1n, -53], // rounding carries into the next exponent at 2
    [(1n << 54n) - 1n, 970], // overflow threshold
  ];
  return midpoints.flatMap(([coefficient, exponent]) => {
    const places = Math.max(0, -exponent);
    const digits = exponent < 0 ? coefficient * 5n ** BigInt(places) : coefficient << BigInt(exponent);
    return [-1n, 0n, 1n].flatMap((offset) => {
      const decimal = `${digits * 10n + offset}e-${places + 1}`;
      return [decimal, `-${decimal}`];
    });
  });
}

export const cases = [
  {
    name: "RegExp slots survive prototype changes while flags observes overridden properties",
    source: `
      const rx = /ab/dgimsy;
      const proto = Object.getPrototypeOf(rx);
      console.log(rx.source, rx.flags, rx.hasIndices, rx.unicode, rx.unicodeSets, rx.lastIndex);
      console.log(proto === Object.getPrototypeOf(/z/u), proto.source, proto.flags, proto.global);
      Object.defineProperty(rx, "global", { get() { this.visits = 1; return false; } });
      Object.defineProperty(rx, "ignoreCase", { get() { this.visits += 1; return true; } });
      console.log(rx.flags, rx.visits);
      try { delete rx.lastIndex; } catch (e) { console.log("nonconfigurable"); }
      const plain = Object.create(proto);
      try { console.log(plain.source); } catch (e) { console.log("brand check"); }
      Object.setPrototypeOf(rx, null);
      console.log(rx.source, rx.lastIndex);
      Object.setPrototypeOf(rx, proto);
      console.log(rx.source);
    `,
  },
  {
    name: "object spread snapshots own keys, then checks each descriptor before reading",
    end: "normal",
    source: `
      const source = {
        __proto__: { inherited: 1 },
        get 10() { console.log("ten"); return this.marker; },
        get 2() {
          console.log("two");
          delete this.gone;
          this.added = 1;
          Object.defineProperty(this, "hidden", { enumerable: false });
          return 2;
        },
        gone: 3, hidden: 4, marker: 5,
      };
      Object.defineProperty(source, "__proto__", { value: 6, enumerable: true });
      const target = { set marker(v) { throw "setter called"; }, ...source, 10: 9 };
      console.log(target[2], target[10], target.marker, target.__proto__, Object.getPrototypeOf(target) === Object.getPrototypeOf({}));
      console.log("inherited" in target, "gone" in target, "added" in target, "hidden" in target);
      target.marker = 7;
      console.log(source.marker, target.marker, delete target.marker);
    `,
  },
  {
    name: "object spread evaluates operands once and stops on a throwing getter",
    end: "normal",
    source: `
      const source = { get value() { console.log("getter"); throw "stop"; } };
      function operand() { console.log("operand"); return source; }
      try { const result = { first: console.log("before"), ...operand(), last: console.log("after") }; }
      catch (e) { console.log(e); }
      const mixed = { ...null, ...false, ...42, ...undefined, ..."éø", ...["x"] };
      console.log(mixed[0], mixed[1], "length" in mixed);
    `,
  },
  {
    name: "var destructuring evaluates its RHS once and writes earlier targets before a getter throws",
    end: "normal",
    source: `let calls = 0;
      function rhs() { calls++; return { first: 4, get second() { console.log(a); throw 3; } }; }
      try { var { first: a, second: b } = rhs(); } catch (e) { console.log(calls, a, b, e); }
      try { var { x } = null; } catch (e) { console.log("null rejected", x); }`,
  },
  {
    name: "getter on a prototype three levels up sees the receiver",
    source: `
      const base = { get who() { return this.name; }, name: "base" };
      const middle = Object.create(base);
      const upper = Object.create(middle);
      const leaf = Object.create(upper);
      leaf.name = "leaf";
      console.log(leaf.who, upper.who, Object.getPrototypeOf(Object.getPrototypeOf(leaf)) === middle);
    `,
  },
  {
    name: "setter on a prototype runs with the receiver and can throw",
    source: `
      const base = { set v(x) { this.seen = x; if (x > 1) throw "too big"; } };
      const o = Object.create(base);
      o.v = 1;
      console.log(o.seen, "v" in o, Object.getPrototypeOf(o) === base);
      try { o.v = 2; } catch (e) { console.log("caught", e, o.seen); }
      o.v = 3;
    `,
  },
  {
    name: "finally overrides a return and a throw",
    source: `
      function a() { try { return "try"; } finally { return "finally"; } }
      function b() { try { throw "thrown"; } finally { return "swallowed"; } }
      function c() { for (let i = 0; i < 3; i++) { try { return i; } finally { if (i < 2) continue; } } return "end"; }
      function d() { let log = ""; try { try { throw 1; } finally { log += "inner "; } } catch (e) { log += "outer " + e; } return log; }
      console.log(a(), b(), c(), d());
    `,
  },
  {
    name: "delete of a missing, an own and a fixed property",
    source: `
      const o = { a: 1 };
      console.log(delete o.missing, delete o.a, o.a, "a" in o);
      Object.defineProperty(o, "fixed", { value: 1 });
      try { delete o.fixed; } catch (e) { console.log("cannot delete", o.fixed); }
      const child = Object.create({ inherited: 1 });
      console.log(delete child.inherited, child.inherited);
    `,
  },
  {
    name: "a method called with a different this",
    source: `
      const a = { name: "a", who() { return this.name; }, arrow: () => typeof this };
      const b = { name: "b", who: a.who };
      const free = a.who;
      console.log(a.who(), b.who(), a.arrow(), (0, a.who) === free);
      try { free(); } catch (e) { console.log("no this"); }
      function Maker() { this.made = true; }
      console.log(new Maker().made, typeof Maker.prototype, Maker.prototype.constructor === Maker);
    `,
  },
  {
    name: "closures capture a loop variable per turn",
    source: `
      const lets = {}; const vars = {};
      for (let i = 0; i < 3; i++) lets[i] = () => i;
      for (var j = 0; j < 3; j++) vars[j] = () => j;
      console.log(lets[0](), lets[1](), lets[2](), vars[0](), vars[2]());
      const steps = {};
      for (let k = 0; k < 2; k++) { steps[k] = () => k; k += 0; }
      let n = 0;
      for (let m = 0; m < 3; m++) { const read = () => m; m++; n += read(); }
      console.log(steps[0](), steps[1](), n);
    `,
  },
  {
    name: "var is hoisted out of a block, function declarations are made on entry",
    source: `
      console.log(typeof early, hoisted, later());
      { var hoisted = 1; }
      if (hoisted) { var early = 2; }
      function later() { return typeof inner + " " + deep; function inner() {} if (false) { var deep; } }
      console.log(early, hoisted);
    `,
  },
  {
    name: "a read in the temporal dead zone throws",
    source: `
      console.log("before");
      { try { tdz; } catch (e) { console.log("caught in block"); } let tdz = 1; console.log(tdz); }
      const f = () => late;
      f();
      let late = 1;
    `,
  },
  {
    name: "typeof a name in the dead zone throws, of an undeclared name does not",
    source: `
      console.log(typeof nowhere, typeof undefined, typeof null, typeof (() => 1), typeof {}, typeof "", typeof 1, typeof true);
      typeof soon;
      const soon = 1;
    `,
  },
  {
    name: "an undeclared name: a read and an assignment throw",
    source: `
      try { missing = 1; } catch (e) { console.log("assignment"); }
      missing;
    `,
  },
  {
    name: "assignment to a constant and to a function's own name",
    source: `
      const c = 1;
      try { c = 2; } catch (e) { console.log("const", c); }
      const f = function self() { try { self = 1; } catch (e) { return typeof self; } };
      console.log(f(), f.name, f.length, (function (a, b) {}).length, (() => {}).name);
      c++;
    `,
  },
  {
    name: "defineProperty: data and accessor descriptors, and what a fixed property refuses",
    source: `
      const o = {};
      Object.defineProperty(o, "ro", { value: 1 });
      try { o.ro = 2; } catch (e) { console.log("read only", o.ro); }
      Object.defineProperty(o, "ro", { value: 1 });
      try { Object.defineProperty(o, "ro", { value: 2 }); } catch (e) { console.log("fixed"); }
      try { Object.defineProperty(o, "ro", { get() { return 3; } }); } catch (e) { console.log("no accessor"); }
      let hidden = 5;
      Object.defineProperty(o, "acc", { get() { return hidden; }, set(v) { hidden = v * 2; }, configurable: true });
      o.acc = 4;
      console.log(o.acc, hidden);
      Object.defineProperty(o, "acc", { value: "data now" });
      console.log(o.acc);
      Object.defineProperty(o, "getOnly", { get: () => 1 });
      try { o.getOnly = 2; } catch (e) { console.log("no setter"); }
      try { Object.defineProperty(o, "bad", { get() {}, value: 1 }); } catch (e) { console.log("both"); }
      try { Object.defineProperty(1, "x", {}); } catch (e) { console.log("not an object"); }
      const desc = { get value() { console.log("reads value"); return 7; }, writable: 1 };
      console.log(Object.defineProperty(o, "viaGetter", desc) === o, o.viaGetter);
      o.viaGetter = 8;
      console.log(o.viaGetter, Object.defineProperty(o, "nan", { value: NaN }) === Object.defineProperty(o, "nan", { value: NaN }));
      try { Object.defineProperty(o, "zero", { value: 0 }); Object.defineProperty(o, "zero", { value: -0 }); } catch (e) { console.log("zeros differ"); }
    `,
  },
  {
    name: "a getter on Object.prototype is found from every object",
    source: `
      const top = Object.getPrototypeOf({});
      console.log(Object.getPrototypeOf(top), Object.getPrototypeOf(() => 1) === Object.getPrototypeOf(function () {}));
      Object.defineProperty(top, "inherited", { get() { return typeof this; }, configurable: true });
      const f = () => 1;
      console.log({}.inherited, f.inherited, Object.create(null).inherited, {}.other);
      delete top.inherited;
      console.log({}.inherited);
    `,
  },
  {
    name: "Object.create with a null prototype refuses unmodeled property descriptors",
    source: `console.log(Object.create(null, { x: { value: 7 } }).x);`,
    end: "unknown",
  },
  {
    name: "setPrototypeOf: a new chain, a cycle, a primitive",
    source: `
      const a = { from: "a" }; const b = { from: "b" }; const o = Object.create(a);
      console.log(o.from, Object.setPrototypeOf(o, b) === o, o.from, Object.setPrototypeOf(1, null));
      try { Object.setPrototypeOf(b, o); } catch (e) { console.log("cycle"); }
      Object.setPrototypeOf(o, null);
      console.log(o.from, Object.getPrototypeOf(o));
      try { Object.setPrototypeOf(o, 1); } catch (e) { console.log("not an object"); }
      try { Object.create(1); } catch (e) { console.log("not a prototype"); }
      Object.getPrototypeOf(undefined);
    `,
  },
  {
    name: "object literals: __proto__, accessors, shorthand, computed and numeric keys",
    source: `
      const proto = { inherited: 1 }; const k = "computed"; const short = 2;
      const o = { __proto__: proto, short, [k + 1]: 3, 1: "one", 1.5: "half", get acc() { return 4; }, set acc(v) {}, m() { return 5; } };
      const nulled = { __proto__: null }; const ignored = { __proto__: 7 }; const own = { ["__proto__"]: 8 };
      console.log(o.inherited, o.short, o.computed1, o[1], o["1.5"], o.acc, o.m(), o.m.name, Object.getPrototypeOf(nulled));
      console.log(Object.getPrototypeOf(ignored) === Object.getPrototypeOf({}), own.__proto__ === 8, o[0.5 + 1]);
      const dup = { a: 1, get a() { return 2; }, b: 3, b: 4 };
      console.log(dup.a, dup.b);
    `,
  },
  {
    name: "calls: what is not a function, new, and the order of evaluation",
    source: `
      let log = "";
      const note = (x) => { log += x; return x; };
      try { (note("f"), undefined)(note("a")); } catch (e) { log += "!"; }
      try { ({}).nope(note("b")); } catch (e) { log += "!"; }
      try { null[note("k")]; } catch (e) { log += "!"; }
      try { null.x = note("r"); } catch (e) { log += "!"; }
      try { new (() => 1)(); } catch (e) { log += "!"; }
      try { new ({ m() {} }).m(); } catch (e) { log += "!"; }
      function Returns() { this.a = 1; return { b: 2 }; }
      function Ignores() { this.a = 1; return 3; }
      console.log(log, new Returns().b, new Ignores().a);
    `,
  },
  {
    name: "classes: methods, accessors, fields and statics, and the names they give",
    source: `
      let order = "";
      class A {
        own = (order += "field ", 1);
        static count = (order += "static ", 0);
        static make(v) { A.count++; return new A(v); }
        constructor(v) { order += "ctor "; this.v = v; }
        get twice() { return this.v * 2; }
        set twice(x) { this.v = x / 2; }
        describe() { return this.own + ":" + this.v; }
        arrow = () => this.v;
      }
      const a = A.make(4);
      a.twice = 10;
      const arrow = a.arrow;
      console.log(order, a.describe(), a.twice, arrow(), A.count, A.name, A.length, typeof A, a.constructor === A);
      console.log(Object.getPrototypeOf(a) === A.prototype, "describe" in a, delete a.own, a.own);
      const B = class { static who = this.name; };
      const C = class Named { static self = Named; };
      console.log(B.who, C.self === C, C.name, (class {}).name);
      try { A(); } catch (e) { console.log("needs new"); }
      try { new Early(); } catch (e) { console.log("dead zone"); }
      class Early {}
      A.prototype = 1;
    `,
  },
  {
    name: "arrays: literals, indices, length",
    source: `
      const a = [1, "two", [3]];
      console.log(a[0], a[1], a[2][0], a.length, a[3], a["1"], 1 in a, 5 in a, typeof a, typeof a.push);
      console.log(delete a[0], a[0], a.length, Object.getPrototypeOf(a) === Object.getPrototypeOf([]));
    `,
  },
  {
    name: "numbers print as Node prints them",
    source: `
      console.log(0.1 + 0.2, 1 / 3, 1e21, 1e-7, 123456789012345680000, 5e-324, 1.7976931348623157e308, -0, 0, NaN, -Infinity);
      console.log(String(-0), "" + -0, 100, 1e5, 0.000001, 1.5e-10, 9007199254740992, 9007199254740994, 4.35, 0.1 * 3, 9007199254740993);
      console.log(5 % 3, -5 % 3, 5 % -3, 5.5 % 2, -0 % 1, 1 % 0, 1e300 % 7, 0.5 % 0.1, -1e-320 % 3e-321);
      console.log(7 >> 1, -7 >> 1, -7 >>> 28, 1 << 31, 1 << 32, ~5, 5 & 3, 5 | 3, 5 ^ 3, 4294967301 | 0, -1 >>> 0, 1e21 | 0);
    `,
  },
  {
    name: "conversions between primitives",
    source: `
      console.log("3" * "4", "0x10" - 0, " 12 " - 0, "1e3" - 0, "" - 0, "abc" - 0, ".5" - 0, "5." - 0, "-Infinity" - 0, "1_0" - 0);
      console.log(1 + "2", "2" + 1, 1 + true, null + 1, undefined + 1, "a" + null, "a" + undefined, true + "x", +"  ", -"");
      console.log(1 == "1", 0 == "", null == undefined, null == 0, "1" == true, NaN == NaN, "a" === "a", 0 === -0, 1 != "1");
      console.log("a" < "b", "a" < "B", "10" < "9", 10 < "9", "x" < 1, null <= 0, undefined <= 0, "abc" >= "ab", 2 > "1");
      console.log(!"", !"0", !0, !NaN, !null, !{}, !-0, void 0, typeof void 0, "abc".length, "abc"[1], "é".length);
      console.log(null ?? "n", undefined ?? "u", 0 ?? "z", 0 || "o", 1 && "a", "" && "never", String(null), \`\${1}-\${true}-\${null}\`);
    `,
  },
  {
    name: "string subtraction rounds at binary64 boundaries",
    end: "normal",
    source: `
      const inputs = ${JSON.stringify(roundingBoundaryStrings())};
      for (let i = 0; i < inputs.length; i++) {
        const rounded = inputs[i] - 0;
        console.log(rounded, 1 / rounded);
      }
    `,
  },
  {
    name: "assignment operators evaluate the target once",
    source: `
      let reads = 0;
      const o = { n: 1 };
      const pick = () => { reads++; return o; };
      pick().n += 2; pick().n++; --pick().n; pick().n *= 2; pick().n ||= 5; pick().n &&= 6; pick().missing ??= 7;
      let x = 5;
      const before = x++ + x; const after = --x - x--;
      console.log(o.n, o.missing, reads, x, before, after, (x += 2, x *= 3), x %= 4, x <<= 2, x ^= 1);
      let s = "a"; s += 1; s += null;
      let u; u ??= "set"; u ??= "kept"; let z = 0; z ||= "or"; z &&= "and";
      console.log(s, u, z);
    `,
  },
  {
    name: "a thrown primitive ends the program with its value",
    source: `
      console.log("printed");
      throw 1.5;
    `,
  },
  {
    name: "a thrown object ends the program",
    source: `
      try { throw { code: 1 }; } catch (e) { console.log(e.code); }
      const f = () => { throw f; };
      f();
    `,
  },
  {
    name: "break and continue leave through finally",
    source: `
      let log = "";
      for (let i = 0; i < 4; i++) {
        try { if (i === 1) continue; if (i === 3) break; log += i; } finally { log += "f"; }
      }
      let w = 0;
      while (true) { if (++w > 2) break; }
      console.log(log, w);
    `,
  },
  {
    name: "a call of a host function the world does not answer for is unknown code",
    source: `console.log("before"); const r = JSON.stringify(4); console.log("after", r);`,
    allowHost: true,
    end: "unknown",
  },
  {
    name: "indirect eval stops replay after evaluating its argument",
    source: `
      console.log("before");
      const value = (0, eval)("1 + " + Number(-0));
      console.log("after", Math.abs(value));
    `,
    end: "unknown",
    expectedStop: {
      detail: "host function eval is called",
      left: 1,
      out: ["before"],
    },
  },
  {
    name: "the world: methods of a string",
    source: `
      const s = "  Hello, World  ";
      const t = s.trim();
      console.log(t.toUpperCase(), t.toLowerCase(), t.slice(1, -1), t.slice(-5), t.substring(7), t.at(-1));
      console.log(t.indexOf("o"), t.lastIndexOf("o"), t.includes("World"), t.startsWith("Hell"), t.endsWith("x"));
      console.log(t.charAt(1), t.charCodeAt(1), t.codePointAt(0), t.padStart(15, "*"), t.padEnd(15, "-") + "|");
      console.log("ab".repeat(3), "a-b-c".replace("-", "+"), "a-b-c".replaceAll("-", "+"), "abc".concat(1, null, true));
      console.log("b".localeCompare("a"), "x".search("x"), "\u00e9".normalize("NFD").length, t.length, t[0], t[99]);
    `,
  },
  {
    name: "the world: numbers, Math and the parsing functions",
    source: `
      const n = 1234.5678;
      console.log(n.toFixed(2), n.toString(16), n.toString(2).length, n.toPrecision(3), n.toExponential(1), (255).toString(36));
      console.log(Math.abs(-3), Math.max(1, "5", 3), Math.min(), Math.floor(-1.5), Math.round(2.5), Math.sign(-0), Math.hypot(3, 4));
      console.log(Math.imul(65537, 65537), Math.clz32(1), Math.trunc(-7.9), Math.sqrt(2), Math.pow(2, 0.5), Math.atan2(1, 1));
      console.log(parseInt("42px", 10), parseInt("ff", 16), parseFloat("3.5e2x"), isNaN("x"), isFinite("12"), Number("0x10"), Number(""));
      console.log(Number.isInteger(5.0), Number.isSafeInteger(2 ** 3), Number.isNaN(NaN), Number.parseFloat("1.5"), Boolean(""), Boolean("0"));
      console.log(String.fromCharCode(72, 105), String.fromCodePoint(955), true.toString(), (false).valueOf(), (5).valueOf());
      const r = Math.random();
      console.log(r >= 0, r < 1, Math.random() === r);
    `.replace("2 ** 3", "8"),
  },
  {
    name: "the world: a host function throws, caught and not",
    source: `
      try { "x".repeat(-1); } catch (e) { console.log("repeat caught"); }
      try { (1).toFixed(101); } catch (e) { console.log("toFixed caught"); }
      try { "x".normalize("bad"); } catch (e) { console.log("normalize caught"); }
      try { String.fromCodePoint(-1); } catch (e) { console.log("fromCodePoint caught"); }
      const slice = "abc".slice;
      try { slice(1); } catch (e) { console.log("no receiver"); }
      console.log("before");
      (1).toString(99);
    `,
    end: "throw",
  },
  {
    name: "the world: calls in a chain, each on the result of the one before",
    source: `
      console.log("abc".toString(), (12).toString(), true.toString());
      console.log("a".concat("b").concat("c").length, "abc".slice(1).slice(1), "  x ".trim().padStart(3, "0"));
    `,
  },
  {
    name: "a method of a string called on an object hands the host an object",
    source: `const o = { upper: "x".toUpperCase }; console.log("before"); o.upper();`,
    end: "unknown",
  },
  {
    name: "a property a string does not have is looked up on Object.prototype",
    source: `
      const top = Object.getPrototypeOf({});
      console.log("abc".missing, (5).missing, true.missing, "abc"[7], "abc"["01"]);
      top.missing = "found";
      Object.defineProperty(top, "shout", { get() { return typeof this + ":" + this; }, configurable: true });
      console.log("abc".missing, (5).missing, true.missing, "abc".shout, (5).shout, false.shout);
      top[7] = "seven";
      console.log("abc"[7], "abc"[1], "abc".length, "".length);
      delete top.missing;
      delete top.shout;
      delete top[7];
    `,
  },
  {
    name: "the iterator symbol is a distinct inherited method key",
    source: `
      class Box {
        value = 7;
        [Symbol.iterator]() { return this.value; }
        "Symbol.iterator"() { return "string"; }
      }
      const box = new Box();
      const sym = Symbol.iterator;
      const record = { ...{ [sym]: 3, "Symbol.iterator": 5 }, [Symbol.iterator]() { return this["Symbol.iterator"]; } };
      console.log(typeof sym, Symbol.iterator === sym, sym === "Symbol.iterator", sym in []);
      console.log(sym in box, box[sym](), box[sym].name, box["Symbol.iterator"]());
      console.log(record[sym](), record[sym].name, record["Symbol.iterator"]);
      box[sym] = 8;
      console.log(box[sym], box["Symbol.iterator"](), delete box[sym], box[sym]());
      function local() {
        const Symbol = { iterator: "plain" };
        return ({ [Symbol.iterator]: 4 }).plain;
      }
      console.log(local());
    `,
    end: "normal",
  },
  {
    name: "fresh symbol brands with the same description have independent instance fields",
    source: `
      const brand = Symbol("brand");
      const other = Symbol("brand");
      let field = brand;
      class Box {
        [field] = true;
        [other] = false;
        "brand" = "string";
      }
      field = other;
      const a = new Box();
      const b = new Box();
      a[brand] = 42;
      console.log(typeof brand, brand === other, brand === "brand", Symbol() === Symbol());
      console.log(brand in a, other in a, Symbol("brand") in a, Symbol.iterator in a);
      console.log(a[brand], a[other], a.brand, b[brand]);
      console.log(delete a[other], other in a, other in b);
      function local() {
        const Symbol = (description) => description;
        return Symbol("shadowed");
      }
      console.log(local());
    `,
    end: "normal",
  },
  {
    name: "a thrown fresh symbol is reported as a symbol",
    source: `throw Symbol("brand");`,
    end: "throw",
  },
  {
    name: "a native host call with a fresh symbol stops at the boundary",
    source: `console.log("before"); Boolean(Symbol());`,
    end: "unknown",
    expectedStop: { detail: "host function Boolean is called", left: 0, out: ["before"] },
  },
  {
    name: "a thrown iterator symbol stays distinct from a string",
    source: `throw Symbol.iterator;`,
    end: "throw",
  },
  {
    name: "a native host call with an iterator symbol argument stops at the boundary",
    source: `console.log("before"); Boolean(Symbol.iterator);`,
    end: "unknown",
    expectedStop: { detail: "host function Boolean is called", left: 0, out: ["before"] },
  },
  {
    name: "the host is handed an object",
    source: `console.log("abc".slice(1)); console.log("abc".replace("b", () => "x"));`,
    end: "unknown",
  },
  {
    name: "host recordings preserve supplementary receivers, arguments and results",
    source: `
      const s = String.fromCodePoint(0x10000);
      console.log(s, s.isWellFormed(), "x".concat(s), "a𐀀".indexOf(s), "a𐀀b".slice(1, 3));
      console.log(s === "𐀀", s.charCodeAt(0), s.charCodeAt(1), s.codePointAt(0));
    `,
    end: "normal",
  },
  {
    name: "a supplementary string from the host can be thrown",
    source: `throw String.fromCodePoint(0x10000);`,
    end: "throw",
  },
  {
    name: "a reached host result containing an isolated surrogate stops replay at that call",
    source: `
      console.log("before");
      const s = String.fromCharCode(0xd800);
      console.log(s.concat(s).toWellFormed());
    `,
    end: "unknown",
    expectedStop: {
      detail: "the host returns a string containing an isolated UTF-16 surrogate",
      left: 2,
      out: ["before"],
    },
  },
  {
    name: "a member of a host global the world does not answer for",
    source: `console.log(Math.abs(-1)); console.log(Math.PI);`,
    end: "unknown",
  },
  {
    name: "a host function is a value: stored, passed, called later",
    source: `
      const fns = { abs: Math.abs, parse: parseInt };
      const apply = (f, x) => f(x);
      console.log(apply(fns.abs, -2), apply(fns.parse, "7.9"), apply(Number, "3"), [Math.max][0](1, 2));
    `,
  },
  {
    name: "a host value can be stored and passed, but not inspected",
    source: `const m = Math; const o = { m }; const same = (x) => x; same(o.m); console.log("stored"); console.log(typeof m);`,
    allowHost: true,
    end: "unknown",
  },
  {
    name: "a method of Object.prototype is host code",
    source: `console.log("before"); ({}).hasOwnProperty("a");`,
    end: "unknown",
  },
  {
    name: "the conversion of an object to a primitive is not modelled",
    source: `console.log("before"); const o = { valueOf() { return 1; } }; console.log(o + 1);`,
    end: "unknown",
  },
  {
    name: "console.log of an object is the host's formatting",
    source: `console.log("before"); console.log({});`,
    end: "unknown",
  },
  {
    name: "a caught engine error is opaque",
    source: `try { null.x; } catch (e) { console.log(typeof e); console.log(e.message); }`,
    end: "unknown",
  },
  {
    name: "a write to an array is not modelled",
    source: `const a = [1]; console.log(a[0]); a[1] = 2;`,
    end: "unknown",
  },
];

// Splits for `differential.mjs`: what `lowersplit` must say of `spec` for the bundle `source`. `refused`: it rejects
// the split. Otherwise it accepts it, and with `differs` the split does not behave like the bundle.
export const bindingSplitCases = [
  {
    name: "certified independent assignments retain their declaration homes",
    poisonWrite: 2,
    source: `let x = 0; let y = 0; x = 1; y = 2; console.log(x, y);`,
    spec: { entry: "entry.mjs", modules: { "m0.mjs": ["#1", "#3"], "entry.mjs": ["#0", "#2", "#4"] } },
    certified: true,
  },
  {
    name: "certified sequencing copies a reference without inspecting its heap",
    source: `const object = { value: 7 }; let x = null; let y = null;
      (x = object, x); (y = object, y); console.log(x.value, y.value);`,
    spec: {
      entry: "entry.mjs",
      modules: {
        "object.mjs": ["#0"],
        "m0.mjs": ["#2", "#4"],
        "entry.mjs": ["#1", "#3", "#5"],
      },
    },
    certified: true,
  },
  {
    name: "certified binary equality copies object identities without reading the heap",
    source: `const object = {}; let x = false; let y = 0;
      x = object === object; y = 3 + 4; console.log(x, y);`,
    spec: {
      entry: "entry.mjs",
      modules: {
        "object.mjs": ["#0"],
        "m0.mjs": ["#2", "#4"],
        "entry.mjs": ["#1", "#3", "#5"],
      },
    },
    certified: true,
  },
  {
    name: "typeof remains outside the binding footprint certificate",
    source: `let x = 0; let y = 0; x = typeof x; y = 1; console.log(x, y);`,
    spec: { entry: "entry.mjs", modules: { "m0.mjs": ["#1", "#3"], "entry.mjs": ["#0", "#2", "#4"] } },
    uncertified: true,
  },
  {
    name: "a binding dependency prevents reordering assignments",
    source: `let x = 0; let y = 0; x = 1; y = x; console.log(x, y);`,
    spec: { entry: "entry.mjs", modules: { "m0.mjs": ["#1", "#3"], "entry.mjs": ["#0", "#2", "#4"] } },
    refused: true,
  },
];

export const splitCases = [
  {
    // A `throw` that the analysis reaches is unknown code, so it keeps its place after the `console.log`.
    name: "a throw does not move before an effect",
    source: `console.log("printed");\nthrow 1.5;\n`,
    spec: { entry: "entry.mjs", modules: { "entry.mjs": ["#0"], "m0.mjs": ["#1"] }, sequential: true },
    refused: true,
  },
  {
    // Premise P4 of `../effects/README.md`: the tool's verdict is for a bundle whose evaluation completes. An
    // exception that the language raises itself in pure code is not an effect to the analysis, so the read of a
    // property of `undefined` may move into a module that runs before the `console.log`. The split is accepted and
    // prints nothing. `differs`: the test expects exactly that, and that the model agrees with Node on both programs.
    name: "a TypeError of pure code moves before an effect",
    source: `const o = { a: undefined };\nconsole.log("printed");\nconst w = o.a.b;\n`,
    spec: { entry: "entry.mjs", modules: { "entry.mjs": ["#1"], "m0.mjs": ["#0", "#2"] } },
    differs: true,
  },
];
