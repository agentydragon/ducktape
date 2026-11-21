// Programs of several modules for `differential.mjs`. Each runs in Node and in the model from `main.mjs` and must print
// the same and end the same way. `end` is how both must end, when that is the point of the case.
export const moduleCases = [
  {
    name: "a live binding is seen after the exporter assigns it",
    files: {
      "main.mjs": `
        import { count, bump, read } from "./counter.mjs";
        console.log(count, read());
        bump();
        console.log(count, read());
        bump();
        console.log(count + 1);
      `,
      "counter.mjs": `
        export let count = 0;
        export function bump() { count++; }
        export function read() { return count; }
      `,
    },
  },
  {
    name: "a function is imported and called before the body of its module ran",
    files: {
      "main.mjs": `
        import { a } from "./a.mjs";
        console.log("main", a());
      `,
      "a.mjs": `
        import { b } from "./b.mjs";
        console.log("a body");
        export function a() { return "a+" + b(); }
      `,
      "b.mjs": `
        import { a } from "./a.mjs";
        console.log("b body", a());
        export function b() { return "b"; }
      `,
    },
  },
  {
    name: "the dead zone across modules",
    files: {
      "main.mjs": `
        import { late, peek } from "./late.mjs";
        console.log("main", late, peek());
      `,
      "late.mjs": `
        import "./early.mjs";
        export const late = "set";
        export function peek() { return late; }
      `,
      "early.mjs": `
        import { late, peek } from "./late.mjs";
        try { console.log(late); } catch (e) { console.log("read too early"); }
        try { console.log(typeof late); } catch (e) { console.log("typeof too early"); }
        try { console.log(peek()); } catch (e) { console.log("through a function too early"); }
        console.log(typeof peek, typeof missing);
      `,
    },
  },
  {
    name: "the dead zone across modules, not caught",
    end: "throw",
    files: {
      "main.mjs": `
        import { late } from "./late.mjs";
        console.log("not reached", late);
      `,
      "late.mjs": `
        import "./early.mjs";
        console.log("not reached");
        export class late {}
      `,
      "early.mjs": `
        import { late } from "./late.mjs";
        console.log("before");
        new late();
      `,
    },
  },
  {
    name: "var across modules is undefined until its declaration runs",
    files: {
      "main.mjs": `
        import { v, set } from "./vars.mjs";
        console.log("main", v);
        set(3);
        console.log("main", v);
      `,
      "vars.mjs": `
        import "./reader.mjs";
        export var v = 1;
        export function set(x) { v = x; }
      `,
      "reader.mjs": `
        import { v, set } from "./vars.mjs";
        console.log("reader", v);
        set(2);
        console.log("reader", v);
      `,
    },
  },
  {
    name: "bare imports run in source order, depth first",
    files: {
      "main.mjs": `
        import "./x.mjs";
        import "./y.mjs";
        import "./z.mjs";
        console.log("main");
      `,
      "x.mjs": `import "./z.mjs"; console.log("x");`,
      "y.mjs": `import "./x.mjs"; console.log("y");`,
      "z.mjs": `console.log("z");`,
    },
  },
  {
    name: "a cycle of three",
    files: {
      "main.mjs": `
        import { a } from "./a.mjs";
        console.log("main", a(3));
      `,
      "a.mjs": `
        import { b } from "./b.mjs";
        console.log("a");
        export function a(n) { return n === 0 ? "a" : "a" + b(n - 1); }
      `,
      "b.mjs": `
        import { c } from "./c.mjs";
        console.log("b");
        export function b(n) { return n === 0 ? "b" : "b" + c(n - 1); }
      `,
      "c.mjs": `
        import { a } from "./a.mjs";
        console.log("c", a(4));
        export function c(n) { return n === 0 ? "c" : "c" + a(n - 1); }
      `,
    },
  },
  {
    name: "an export under another name, imported under a third",
    files: {
      "main.mjs": `
        import { outer as mine, twice as inner } from "./m.mjs";
        const outer = "local";
        console.log(mine, inner(mine), outer);
      `,
      "m.mjs": `
        const inner = 21;
        function double(x) { return x * 2; }
        export { inner as outer, double as twice };
      `,
    },
  },
  {
    name: "one module imported twice, and by two modules",
    files: {
      "main.mjs": `
        import { a } from "./m.mjs";
        import { other } from "./other.mjs";
        import { b, a as again } from "./m.mjs";
        console.log(a, b, again, other());
      `,
      "m.mjs": `
        console.log("m runs once");
        export const a = 1;
        export let b = 2;
        export function grow() { b = b + 1; return b; }
      `,
      "other.mjs": `
        import { grow } from "./m.mjs";
        export function other() { return grow() + grow(); }
      `,
    },
  },
  {
    name: "an import of a name the module does not export",
    end: "throw",
    files: {
      "main.mjs": `
        import { present, absent } from "./m.mjs";
        console.log("not reached", present, absent);
      `,
      "m.mjs": `
        console.log("not reached");
        export const present = 1;
        const absent = 2;
      `,
    },
  },
  {
    name: "an import cannot be assigned",
    files: {
      "main.mjs": `
        import { n, zero, late } from "./m.mjs";
        try { n = 5; } catch (e) { console.log("assign", n); }
        try { n++; } catch (e) { console.log("update", n); }
        try { n += (console.log("right side runs"), 1); } catch (e) { console.log("compound", n); }
        console.log(n ||= 7, n);
        try { zero ||= 7; } catch (e) { console.log("logical", zero); }
        function shadow(n) { n = n + 1; return n; }
        console.log(shadow(1), n);
        { let n = "block"; n = n + "!"; console.log(n); }
      `,
      "m.mjs": `
        export let n = 1;
        export let zero = 0;
      `,
    },
  },
  {
    name: "an import in its dead zone cannot be assigned either",
    files: {
      "main.mjs": `
        import "./early.mjs";
        export let late = 1;
      `,
      "early.mjs": `
        import { late } from "./main.mjs";
        try { late = 2; } catch (e) { console.log("assign"); }
        try { late++; } catch (e) { console.log("update"); }
      `,
    },
  },
  {
    name: "assigning an import is a TypeError, also in its dead zone",
    end: "throw",
    files: {
      "main.mjs": `
        import "./early.mjs";
        export let late = 1;
      `,
      "early.mjs": `
        import { late } from "./main.mjs";
        late = (console.log("the right side runs first"), 2);
      `,
    },
  },
  {
    name: "updating an import in its dead zone is a ReferenceError: it is read first",
    end: "throw",
    files: {
      "main.mjs": `
        import "./early.mjs";
        export let late = 1;
      `,
      "early.mjs": `
        import { late } from "./main.mjs";
        late++;
      `,
    },
  },
  {
    name: "default exports: a function, an expression, a class",
    files: {
      "main.mjs": `
        import f, { other } from "./f.mjs";
        import value from "./value.mjs";
        import Shape from "./shape.mjs";
        import anonymous from "./anonymous.mjs";
        console.log(f(2), f.name, other, value, new Shape().sides, Shape.name, anonymous(), anonymous.name);
      `,
      "f.mjs": `
        console.log(named(1));
        export default function named(x) { return x + 1; }
        export const other = "other";
      `,
      "value.mjs": `export default 6 * 7;`,
      "shape.mjs": `export default class { sides = 4; }`,
      "anonymous.mjs": `export default function () { return "anonymous"; }`,
    },
  },
  {
    name: "export before a declaration",
    files: {
      "main.mjs": `
        import { a, b, c, f, C, v } from "./m.mjs";
        console.log(a, b, c, f(), new C().x, v);
      `,
      "m.mjs": `
        export const a = 1, b = a + 1;
        export let c = b + 1;
        export function f() { return c + 1; }
        export class C { x = f() + 1; }
        export var v = 6;
      `,
    },
  },
  {
    name: "a module that throws stops the program",
    end: "throw",
    files: {
      "main.mjs": `
        import "./first.mjs";
        import "./second.mjs";
        import "./third.mjs";
        console.log("not reached");
      `,
      "first.mjs": `console.log("first");`,
      "second.mjs": `console.log("second"); throw "stop"; console.log("not reached");`,
      "third.mjs": `console.log("not reached");`,
    },
  },
  {
    name: "this is undefined in every module, and each module has its own names",
    files: {
      "main.mjs": `
        import { who } from "./a.mjs";
        import { who as other } from "./b.mjs";
        const name = "main";
        console.log(this, who(), other(), name);
      `,
      "a.mjs": `const name = "a"; export const who = () => name + String(this);`,
      "b.mjs": `var name = "b"; export function who() { return name; }`,
    },
  },
  {
    name: "a module in a directory imports relative to itself",
    files: {
      "main.mjs": `
        import { deep } from "./lib/deep.mjs";
        console.log(deep);
      `,
      "lib/deep.mjs": `
        import { base } from "../base.mjs";
        import { near } from "./near.mjs";
        export const deep = base + near;
      `,
      "lib/near.mjs": `export const near = "near";`,
      "base.mjs": `export const base = "base+";`,
    },
  },
];
