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

export const opaqueCases = [
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
    return { code, part, steps };
  });
