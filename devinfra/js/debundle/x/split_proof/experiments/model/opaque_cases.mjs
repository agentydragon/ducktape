// Programs with statements outside the model's syntax, for `differential.mjs`. The front end makes those statements
// opaque (`to_core.mjs`, option `opaque`), and the model's world runs them from a script (`Run.lean`): for each part
// of the opaque code of a statement, the requests the world makes. A script has to do what the JavaScript does, so the
// model and Node print the same and end the same way; each case is also split into consecutive modules, and the split
// must end in the bundle's state.
//
// `scripts` maps `"identity/part"` to requests `[op, operand…]`. Part 0 is the statement; part `k` is its `k`-th
// function declaration, then the functions it makes when it only makes functions (no part 0 then), or a function the
// world made with `newFunction`. A binding is named by its position in the statement's names: what it declares, then
// its functions, then the other names it mentions, in source order.
// An operand is a primitive (`["u"]`, `["n"]`, `["b", true]`, `["d", bits]`, `["s", text]`) or a register,
// `["r", k]`: register 0 is the callee, 1 is `this`, then the arguments, then the answer to each request in turn.
import { bits } from "./to_core.mjs";

const num = (x) => ["d", bits(x)];
const r = (k) => ["r", k];

const keyBoundaryObject = { tail: 1, 10: 10, 2: 2, "01": 1, "-0": 0, 4294967295: 5, 4294967294: 4, 0: 0 };
const beforeReinsert = Object.getOwnPropertyNames(keyBoundaryObject);
delete keyBoundaryObject.tail;
keyBoundaryObject.tail = 2;
const afterReinsert = Object.getOwnPropertyNames(keyBoundaryObject);

export const opaqueCases = [
  {
    name: "opaque class methods capture distinct enclosing function invocations",
    source: `
      function make(value) {
        class Box { read() { const [unused] = []; return value; } }
        return Box;
      }
      const First = make(7);
      const Second = make(9);
      console.log(new First().read(), new Second().read());
    `,
    scripts: {
      "0/2": [
        ["readVar", 0],
        ["done", r(2)],
      ],
    },
    bodyParts: ["0/2"],
  },
  {
    name: "opaque class bodies do not run during moved class creation",
    source: `
      class Box {
        constructor() { const [unused] = []; calls++; throw "eager constructor"; }
        static unused() { const [unused] = []; calls++; throw "eager method"; }
      }
      const independent = 3;
      let calls = 0;
      console.log(calls, independent, Box.name, Box.length);
    `,
    scripts: {},
    bodyParts: ["0/1", "0/2"],
    move: true,
  },
  {
    name: "opaque class constructor and methods receive receivers and original arguments",
    source: `
      class Box {
        constructor(value, extra) { const [v] = [value]; this.value = v; this.extra = extra; }
        read() { const [v] = [this.value]; return v; }
        static read(value) { const [v] = [value]; return v; }
        receiver() { const [unused] = []; return this; }
      }
      const box = new Box(7, 9);
      const receiver = box.receiver;
      console.log(box.read(), box.extra, Box.read(11), box.constructor === Box, receiver() === undefined);
    `,
    scripts: {
      "0/1": [
        ["set", r(1), ["s", "value"], r(2)],
        ["set", r(1), ["s", "extra"], r(3)],
      ],
      "0/2": [
        ["get", r(1), ["s", "value"]],
        ["done", r(2)],
      ],
      "0/3": [["done", r(2)]],
      "0/4": [["done", r(1)]],
    },
    bodyParts: ["0/1", "0/2", "0/3", "0/4"],
  },
  {
    name: "opaque named class methods retain the immutable inner class name",
    source: `
      let Alias = class Inner { self() { const [unused] = []; return Inner; } };
      const Original = Alias;
      Alias = 0;
      console.log(new Original().self() === Original, Original.name);
    `,
    scripts: {
      "0/1": [
        ["readVar", 0],
        ["done", r(2)],
      ],
    },
    bodyParts: ["0/1"],
  },
  {
    name: "opaque constructors keep fields on primitive return, replace on object return, and propagate throw",
    source: `
      class Keeps { field = 5; constructor() { const [unused] = []; this.seen = this.field; return 3; } }
      class Replaces { constructor(value) { const [v] = [value]; return v; } }
      class Fails { constructor(value) { const [v] = [value]; throw v; } }
      const replacement = { value: 8 };
      console.log(new Keeps().seen, new Replaces(replacement) === replacement);
      try { new Fails(13); } catch (e) { console.log("constructor caught", e); }
    `,
    scripts: {
      "0/1": [
        ["get", r(1), ["s", "field"]],
        ["set", r(1), ["s", "seen"], r(2)],
        ["done", num(3)],
      ],
      "1/1": [["done", r(2)]],
      "2/1": [["throw", r(2)]],
    },
    bodyParts: ["0/1", "1/1", "2/1"],
  },
  {
    name: "opaque class constructors are not callable and methods are not constructible",
    source: `
      class Box {
        constructor() { const [unused] = []; throw "body must not run"; }
        method() { const [unused] = []; throw "body must not run"; }
        static method() { const [unused] = []; throw "body must not run"; }
      }
      try { Box(); } catch (e) { console.log("class call caught"); }
      try { new Box.prototype.method(); } catch (e) { console.log("instance method construction caught"); }
      try { new Box.method(); } catch (e) { console.log("static method construction caught"); }
    `,
    scripts: {},
    bodyParts: ["0/1", "0/2", "0/3"],
  },
  {
    name: "opaque class accessors read live captures and receive the instance",
    source: `
      class Box {
        get value() { const [unused] = []; return captured; }
        set value(value) { const [v] = [value]; this.stored = v; }
      }
      let captured = 4;
      const box = new Box();
      captured = 9;
      box.value = 6;
      console.log(box.value, box.stored);
    `,
    scripts: {
      "0/1": [
        ["readVar", 0],
        ["done", r(2)],
      ],
      "0/2": [["set", r(1), ["s", "stored"], r(2)]],
    },
    bodyParts: ["0/1", "0/2"],
  },
  {
    name: "opaque method parts count modeled and nested functions in lexical source order",
    source: `
      class Box {
        modeled() { return 1; }
        opaque() { const helper = () => 2; const [v] = [helper()]; return v; }
        later() { const [v] = [3]; return v; }
      }
      const box = new Box();
      console.log(box.modeled(), box.opaque(), box.later());
    `,
    scripts: { "0/2": [["done", num(2)]], "0/4": [["done", num(3)]] },
    bodyParts: ["0/2", "0/4"],
  },
  {
    name: "opaque class bodies preserve eager static initializers and computed field keys",
    source: `
      let order = "";
      function record(text) { order += text; }
      class Box {
        [(order += "key ", "field")] = (order += "field ", 4);
        static value = (order += "static ", 7);
        constructor() { const [unused] = []; record("body "); }
      }
      console.log(order, Box.value);
      const box = new Box();
      console.log(order, box.field);
    `,
    scripts: {
      "2/1": [
        ["readVar", 0],
        ["call", r(2), ["u"], ["s", "body "]],
      ],
    },
    bodyParts: ["2/1"],
  },
  {
    name: "own key array-index boundaries and string reinsertion follow Node",
    source: `
      const box = { tail: 1, 10: 10, 2: 2, "01": 1, "-0": 0, 4294967295: 5, 4294967294: 4, 0: 0 };
      const [first] = [Object.getOwnPropertyNames(box)[0]];
      delete box.tail;
      box.tail = 2;
      const [last] = [Object.getOwnPropertyNames(box).at(-1)];
      console.log(first, last);
    `,
    scripts: {
      "1/0": [
        ["readVar", 2],
        ["keys", r(2)],
        ["initVar", 0, ["s", "0"]],
      ],
      "4/0": [
        ["readVar", 2],
        ["keys", r(2)],
        ["initVar", 0, ["s", "tail"]],
      ],
    },
    expectedKeys: { "1/0": [beforeReinsert], "4/0": [afterReinsert] },
  },
  {
    name: "opaque own keys match Node ordering and do not invoke accessors",
    source: `
      const box = { z: 1, 7: 7, 2: 2, a: 3, get unused() { throw "getter must not run"; } };
      const [first] = [Object.getOwnPropertyNames(box)[0]];
      console.log(first);
    `,
    scripts: {
      "1/0": [
        ["readVar", 2],
        ["keys", r(2)],
        ["initVar", 0, ["s", "2"]],
      ],
    },
    // Computed by Node itself, independently of CoreJS's ownKeys.
    expectedKeys: {
      "1/0": [
        Object.getOwnPropertyNames({
          z: 1,
          7: 7,
          2: 2,
          a: 3,
          get unused() {
            throw "getter must not run";
          },
        }),
      ],
    },
  },
  {
    name: "a failing opaque property request propagates an engine error to the model catch",
    source: `
      const box = {};
      Object.defineProperty(box, "fixed", { value: 3 });
      function remove(value) { const [removed] = [delete value.fixed]; return removed; }
      try { remove(box); } catch (e) { console.log("delete caught", box.fixed); }
      console.log("after failed request");
    `,
    scripts: {
      "2/1": [
        ["delete", r(2), ["s", "fixed"]],
        ["done", r(3)],
      ],
    },
  },
  {
    name: "opaque property requests observe deletion and inherited membership",
    source: `
      const base = { inherited: 7 };
      const box = { __proto__: base, own: 3 };
      const [removed, inherited, missing] = [delete box.own, "inherited" in box, "own" in box];
      console.log(removed, inherited, missing, box.own, box.inherited);
    `,
    // Names: removed, inherited, missing, box. Each request's answer is observed.
    scripts: {
      "2/0": [
        ["readVar", 3],
        ["delete", r(2), ["s", "own"]],
        ["initVar", 0, r(3)],
        ["has", ["s", "inherited"], r(2)],
        ["initVar", 1, r(5)],
        ["has", ["s", "own"], r(2)],
        ["initVar", 2, r(7)],
      ],
    },
  },
  {
    name: "opaque construction reenters a model constructor",
    source: `
      function Box(n) { this.n = n + 1; }
      const [made] = [new Box(4)];
      console.log(made.n, made.constructor === Box);
    `,
    scripts: {
      "1/0": [
        ["readVar", 1],
        ["construct", r(2), num(4)],
        ["initVar", 0, r(3)],
      ],
    },
  },
  {
    name: "an opaque constructor initializes the receiver allocated by new",
    source: `
      const Box = function(n) { const [v] = [n]; this.n = v; };
      const independent = 3;
      const made = new Box(6);
      console.log(made.n, made.constructor === Box, Box.length, independent);
    `,
    scripts: { "0/1": [["set", r(1), ["s", "n"], r(2)]] },
    move: true,
  },
  {
    name: "opaque code allocates with an explicit prototype and calls a model primitive",
    source: `
      const base = { inherited: 9 };
      const [made, inherited] = [Object.create(base), base.inherited];
      console.log(made.inherited, inherited, Object.getPrototypeOf(made) === base);
    `,
    scripts: {
      "1/0": [
        ["readVar", 3],
        ["newObject", r(2)],
        ["prim", "getPrototypeOf", r(3)],
        ["get", r(4), ["s", "inherited"]],
        ["initVar", 0, r(3)],
        ["initVar", 1, r(5)],
      ],
    },
  },
  {
    name: "the world makes a constructor whose body receives the new object",
    source: `
      const [Box] = [function(n) { this.n = n; }];
      console.log(new Box(11).n, Box.length);
    `,
    scripts: {
      "0/0": [
        ["newFunction", 1, "", 1, true],
        ["initVar", 0, r(2)],
      ],
      "0/1": [["set", r(1), ["s", "n"], r(2)]],
    },
  },
  {
    name: "a model callback throws through the world and is caught by the model",
    source: `
      function invoke(f) { const [v] = [f()]; return v; }
      const fail = () => { throw 13; };
      try { invoke(fail); } catch (e) { console.log("caught callback", e); }
      console.log("after callback");
    `,
    scripts: {
      "0/1": [
        ["call", r(2), ["u"]],
        ["done", r(3)],
      ],
    },
  },
  {
    name: "opaque code reaches a declaration through a captured binding",
    source: `
      const read = () => value;
      const box = { read };
      const value = true;
      const [observed] = [box.read()];
      console.log(observed);
    `,
    // The opaque statement mentions box, not value. Its call reaches value through read's closure.
    // Names of statement 3: observed, box.
    scripts: {
      "3/0": [
        ["readVar", 1],
        ["get", r(2), ["s", "read"]],
        ["call", r(3), r(2)],
        ["initVar", 0, r(4)],
      ],
    },
  },
  {
    name: "moving a declaration past an opaque callback exposes its dead zone",
    source: `
      const read = () => value;
      const box = { read };
      const [observed] = [box.read()];
      const value = true;
      console.log(observed);
    `,
    end: "throw",
    // The same pair as above, reversed. Disjoint direct names do not justify this move.
    scripts: {
      "2/0": [
        ["readVar", 1],
        ["get", r(2), ["s", "read"]],
        ["call", r(3), r(2)],
        ["initVar", 0, r(4)],
      ],
    },
  },
  {
    name: "a destructuring declaration reads an object of the program",
    source: `
      const o = { a: 1, b: 2 };
      const { a, b } = o;
      console.log(a + b);
    `,
    // names of statement 1: a, b, o
    scripts: {
      "1/0": [
        ["readVar", 2],
        ["get", r(2), ["s", "a"]],
        ["initVar", 0, r(3)],
        ["get", r(2), ["s", "b"]],
        ["initVar", 1, r(5)],
      ],
    },
  },
  {
    name: "an opaque function is called by the program and calls back into it",
    source: `
      function twice(f, x) { const [y] = [f(x)]; return f(y); }
      const inc = (n) => n + 1;
      console.log(twice(inc, 1), typeof twice, twice.length);
    `,
    // part 1 of statement 0 is the body of `twice`: register 2 is `f`, 3 is `x`
    scripts: {
      "0/1": [
        ["call", r(2), ["u"], r(3)],
        ["call", r(2), ["u"], r(4)],
        ["done", r(5)],
      ],
    },
  },
  {
    name: "a throw of opaque code is caught by the program",
    source: `
      function fail(x) { for (const c of [x]) throw c; }
      try { fail(7); } catch (e) { console.log("caught", e); }
      console.log("after");
    `,
    scripts: { "0/1": [["throw", r(2)]] },
  },
  {
    name: "an arrow with destructuring and default parameters is made without the world and called by the program",
    source: `
      const pick = ({ k }, d = 0) => k + d;
      console.log(pick({ k: 2 }), pick.name, pick.length, typeof pick);
    `,
    // part 1 of statement 0 is the body of `pick`: register 2 is the first argument
    scripts: {
      "0/1": [
        ["get", r(2), ["s", "k"]],
        ["done", num(2)],
      ],
    },
  },
  {
    name: "opaque code writes a binding and a property that the program reads",
    source: `
      let count = 0;
      const box = { v: 1 };
      switch (count) { default: count = 5; box.v = 2; }
      console.log(count, box.v);
    `,
    // names of statement 2: count, box
    scripts: {
      "2/0": [
        ["readVar", 0],
        ["writeVar", 0, num(5)],
        ["readVar", 1],
        ["set", r(4), ["s", "v"], num(2)],
      ],
    },
  },
  {
    name: "opaque code makes an object and a function that the program uses",
    source: `
      const made = { ...{ k: 1 }, f(n) { const { m } = { m: n * 2 }; return m; } };
      console.log(made.k, made.f(4), Object.getPrototypeOf(made) === Object.getPrototypeOf({}));
    `,
    // part 1 is the method `f`: register 2 is `n`
    scripts: {
      "0/0": [
        ["newObject"],
        ["set", r(2), ["s", "k"], num(1)],
        ["newFunction", 1, "f", 1, false],
        ["set", r(2), ["s", "f"], r(4)],
        ["initVar", 0, r(2)],
      ],
      "0/1": [["done", num(8)]],
    },
  },
  {
    name: "a getter of the program runs when opaque code reads the property",
    source: `
      let reads = 0;
      const o = { get g() { reads++; return "got"; } };
      const [first] = [o.g];
      console.log(first, reads);
    `,
    // names of statement 2: first, o
    scripts: {
      "2/0": [
        ["readVar", 1],
        ["get", r(2), ["s", "g"]],
        ["initVar", 0, r(3)],
      ],
    },
  },
];

/** The scripts of a case as `corejs` reads them. */
export const scriptsOf = (c) =>
  Object.entries(c.scripts).map(([key, steps]) => {
    const [code, part] = key.split("/").map(Number);
    return { code, part, steps, expectedKeys: c.expectedKeys?.[key] ?? [] };
  });
