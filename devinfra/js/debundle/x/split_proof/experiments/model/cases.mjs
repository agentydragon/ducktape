// Programs for `differential.mjs`, for what the generator rarely writes. Each runs in Node and in the model and must
// print the same and end the same way. A case with `end: "unknown"` uses a value of the host (`allowHost`) or an
// operation the model leaves to it; there only the model's ending is checked.
export const cases = [
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
    name: "a call of a host function is unknown code",
    source: `console.log("before"); const r = Math.sqrt(4); console.log("after", r);`,
    allowHost: true,
    end: "unknown",
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
