// Family `unmodeled`: constructs that the analysis has no model for, or models loosely, so that it must treat a unit that
// uses them as touching the world and never as pure. Typed arrays and the views that alias them (`subarray`, `buffer`,
// `DataView`), a global regular expression whose `lastIndex` moves, `Date` setters, `WeakRef`, `WeakMap` and `WeakSet`,
// `eval`, indirect `eval` and `Function` (of code that mentions no name of the program, because a name in a string is not
// renamed), continuations that run after the program (`queueMicrotask`, `process.nextTick`, `Promise.withResolvers`,
// `setTimeout`), `Intl`, `BigInt`, URI coding, `Symbol.for`, copies of arrays (`toSorted`, `with`, `flat`), and the
// `console` methods that print. There is no `setImmediate`: it runs before or after a timer from run to run.
import { kindOf, local } from "./core.mjs";

const TYPED = { Uint8Array: 1, Int8Array: 1, Int16Array: 2, Uint32Array: 4, Float64Array: 8 };

export function install(g) {
  const pick = (xs) => g.pick(xs);
  const N = (s, d = 2) => g.num(s, d);
  const S = (s, d = 2) => g.str(s, d);
  const clamp = (text) => `(${text}).slice(0, 24)`;
  const num = g.prods.num;
  const str = g.prods.str;
  const bool = g.prods.bool;
  const st = g.stmts;
  const of = (s, kind, ok = () => true) => g.refs(s, (b) => kindOf(b.type) === kind && ok(b));
  const index = (s, length) => `(Math.abs(${N(s, 1)} | 0) % ${length})`;
  const list = (s, n) => Array.from({ length: n }, () => N(s, 1)).join(", ");

  // ---- typed arrays, and the views that share their memory
  g.makers.push({
    name: "typed array",
    w: 3,
    make: (g) => {
      const s = g.topScope();
      const kind = pick(Object.keys(TYPED));
      const length = 2 + g.int(3);
      const text = pick([
        `new ${kind}(${length})`,
        `${kind}.of(${list(s, length)})`,
        `${kind}.from([${list(s, length)}])`,
        `new ${kind}([${list(s, length)}])`,
      ]);
      const t = g.binding("t", { t: "typed", kind, length }, { kind: "const" });
      g.add("typed array", `const ${g.ref(t)} = ${text};`, [t]);
    },
  });
  g.makers.push({
    name: "typed view",
    w: 3,
    ok: (g) => of(g.topScope(), "typed", (b) => b.type.length >= 2).length > 0,
    make: (g) => {
      const t = pick(of(g.topScope(), "typed", (b) => b.type.length >= 2));
      const x = g.ref(t);
      const { kind, length } = t.type;
      const forms = [
        [`${x}.subarray(1)`, { t: "typed", kind, length: length - 1 }],
        [`${x}.subarray(0, 1)`, { t: "typed", kind, length: 1 }],
        [`new ${kind}(${x}.buffer)`, { t: "typed", kind, length }],
        [`${x}.slice(1)`, { t: "typed", kind, length: length - 1 }],
        [`${x}.map((y) => y + 1)`, { t: "typed", kind, length }],
        [`new DataView(${x}.buffer)`, { t: "dview", length: length * TYPED[kind] }],
      ];
      const [text, type] = pick(forms);
      const v = g.binding(type.t === "dview" ? "d" : "t", type, { kind: "const" });
      g.add("typed view", `const ${g.ref(v)} = ${text};`, [v]);
    },
  });
  num.push({
    w: 3,
    ok: (g, s) => of(s, "typed").length > 0,
    make: (g, s, d) => {
      const t = pick(of(s, "typed"));
      const x = g.ref(t);
      return pick([
        `${x}[${index(s, t.type.length)}]`,
        `${x}.length`,
        `${x}.byteLength`,
        `${x}.at(-1)`,
        `${x}.indexOf(${N(s, d)})`,
        `${x}.reduce((p, q) => p + q, 0)`,
        `Array.from(${x}).length`,
        `${x}.join(",").length`,
        `Math.max(...${x})`,
      ]);
    },
  });
  num.push({
    w: 2,
    ok: (g, s) => of(s, "dview").length > 0,
    make: (g, s) => {
      const v = pick(of(s, "dview"));
      return `${g.ref(v)}.getUint8(${index(s, v.type.length)})`;
    },
  });
  bool.push({
    w: 1,
    ok: (g, s) => of(s, "typed").length > 0,
    make: (g, s, d) => `${g.ref(pick(of(s, "typed")))}.includes(${N(s, d)})`,
  });
  st.push({
    w: 4,
    ok: (g, s) => of(s, "typed").length > 0,
    make: (g, s) => {
      const t = pick(of(s, "typed"));
      g.touch(s, t);
      const x = g.ref(t);
      const place = `${x}[${index(s, t.type.length)}]`;
      return pick([
        `${place} = ${N(s)};`, `${place} += ${N(s, 1)};`, `${place}++;`, `${x}.fill(${N(s, 1)});`, `${x}.fill(${N(s, 1)}, 0, 1);`,
        `${x}.set([${N(s, 1)}]);`, `${x}.reverse();`, `${x}.sort();`, `${x}.copyWithin(0, 1);`,
      ]); // prettier-ignore
    },
  });
  st.push({
    w: 2,
    ok: (g, s) => of(s, "dview", (b) => b.type.length >= 2).length > 0,
    make: (g, s) => {
      const v = pick(of(s, "dview", (b) => b.type.length >= 2));
      g.touch(s, v);
      return pick([
        `${g.ref(v)}.setUint8(${index(s, v.type.length)}, ${N(s, 1)});`,
        `${g.ref(v)}.setInt16(0, ${N(s, 1)});`,
      ]);
    },
  });
  g.observers.typed = (g, b) => `[...${g.ref(b)}].join(",")`;
  g.observers.dview = (g, b) => `[...new Uint8Array(${g.ref(b)}.buffer)].join(",")`;

  // ---- state in a regular expression, a date, a WeakRef
  g.makers.push({
    name: "global regexp",
    w: 2,
    make: (g) => {
      const r = g.binding("r", { t: "rx" }, { kind: "const" });
      g.add("global regexp", `const ${g.ref(r)} = ${pick(["/a/g", "/[a-c]/gy", 'new RegExp("b", "g")', "/\\w/g"])};`, [
        r,
      ]);
    },
  });
  bool.push({
    w: 2,
    ok: (g, s) => of(s, "rx").length > 0,
    make: (g, s, d) => `${g.ref(pick(of(s, "rx")))}.test(${S(s, d)})`,
  });
  num.push({
    w: 2,
    ok: (g, s) => of(s, "rx").length > 0,
    make: (g, s, d) => {
      const r = g.ref(pick(of(s, "rx")));
      return pick([`${r}.lastIndex`, `((${r}.exec(${S(s, d)}) ?? [""])[0].length)`]);
    },
  });
  st.push({
    w: 2,
    ok: (g, s) => of(s, "rx").length > 0,
    make: (g, s) =>
      pick([`${g.ref(pick(of(s, "rx")))}.lastIndex = 0;`, `${g.ref(pick(of(s, "rx")))}.test(${S(s, 1)});`]),
  });
  g.observers.rx = (g, b) => `String(${g.ref(b)}.lastIndex)`;
  g.makers.push({
    name: "date",
    w: 2,
    make: (g) => {
      const d = g.binding("d", { t: "date" }, { kind: "const" });
      g.add("date", `const ${g.ref(d)} = new Date(${pick(["0", "86400000", "1e12"])});`, [d]);
    },
  });
  num.push({
    w: 2,
    ok: (g, s) => of(s, "date").length > 0,
    make: (g, s, d) =>
      pick([
        `${g.ref(pick(of(s, "date")))}.getTime()`,
        `${g.ref(pick(of(s, "date")))}.getUTCFullYear()`,
        `(+${g.ref(pick(of(s, "date")))})`,
        `Date.UTC(2000, ${N(s, d)} & 7, 1)`,
      ]),
  });
  str.push({
    w: 1,
    ok: (g, s) => of(s, "date").length > 0,
    make: (g, s) => `JSON.stringify(${g.ref(pick(of(s, "date")))})`,
  });
  st.push({
    w: 2,
    ok: (g, s) => of(s, "date").length > 0,
    make: (g, s) =>
      `${g.ref(pick(of(s, "date")))}.${pick(["setTime", "setUTCFullYear", "setUTCMonth", "setUTCDate", "setUTCHours"])}(${N(s, 1)});`,
  });
  g.observers.date = (g, b) => `String(${g.ref(b)}.getTime())`;
  const withNumField = (s) =>
    g.fields(s, "num").filter(([b]) => !s.frozen.has(b.name) && !b.type.fixed && !b.type.counter);
  g.makers.push({
    name: "weak ref",
    w: 2,
    ok: (g) => withNumField(g.topScope()).length > 0,
    make: (g) => {
      const [o] = pick(withNumField(g.topScope()));
      const w = g.binding("w", { t: "weakref", target: o }, { kind: "const" });
      g.add("weak ref", `const ${g.ref(w)} = new WeakRef(${g.ref(o)});`, [w]);
    },
  });
  const refsWithField = (s) =>
    of(s, "weakref").filter(
      (w) => Object.values(w.type.target.type.fields).includes("num") && !s.frozen.has(w.type.target.name)
    );
  num.push({
    w: 2,
    ok: (g, s) => refsWithField(s).length > 0,
    make: (g, s, d) => {
      const w = pick(refsWithField(s));
      const [field] = pick(Object.entries(w.type.target.type.fields).filter(([, t]) => t === "num"));
      return `(${g.ref(w)}.deref()?.[${JSON.stringify(field)}] ?? ${N(s, d)})`;
    },
  });
  g.observers.weakref = () => null;
  // WeakMap and WeakSet are written and read by key; nothing observes them as a whole
  g.makers.push({
    name: "weak collections",
    w: 1,
    make: (g) => {
      const m = g.binding("w", { t: "weakmap" }, { kind: "const" });
      g.add("weak map", `const ${g.ref(m)} = new WeakMap();`, [m]);
    },
  });
  const objects = (s) => g.refs(s, (b) => ["obj", "arr", "map", "set"].includes(kindOf(b.type)) && !b.type.counter);
  st.push({
    w: 2,
    ok: (g, s) => of(s, "weakmap").length > 0 && objects(s).length > 0,
    make: (g, s) => {
      const m = g.ref(pick(of(s, "weakmap")));
      const k = g.ref(pick(objects(s)));
      return pick([`${m}.set(${k}, ${N(s, 1)});`, `${m}.delete(${k});`]);
    },
  });
  num.push({
    w: 1,
    ok: (g, s) => of(s, "weakmap").length > 0 && objects(s).length > 0,
    make: (g, s, d) => `(${g.ref(pick(of(s, "weakmap")))}.get(${g.ref(pick(objects(s)))}) ?? ${N(s, d)})`,
  });
  bool.push({
    w: 1,
    ok: (g, s) => of(s, "weakmap").length > 0 && objects(s).length > 0,
    make: (g, s) => `${g.ref(pick(of(s, "weakmap")))}.has(${g.ref(pick(objects(s)))})`,
  });
  g.observers.weakmap = () => null;

  // ---- code made at run time, from numbers only
  num.push({
    w: 2,
    make: (g, s, d) =>
      pick([
        `eval("(" + Number(${N(s, d)}) + ") * 2")`, // Number: a number expression may be a generator object
        `(0, eval)("1 + " + Number(${N(s, d)}))`,
        `new Function("a", "return a * 2")(${N(s, d)})`,
        `Function("a", "b", "return a - b")(${N(s, d)}, ${N(s, d)})`,
        'new Function("return 7")()',
      ]),
  });

  // ---- continuations that run after the program
  const later = (s, locals = []) => {
    g.late = true;
    return g.block(g.callback(s, locals, { ret: "void" }), 1 + g.int(2));
  };
  st.push({
    w: 3,
    ok: (g, s) => s.nest > 0,
    make: (g, s) => {
      const x = { name: g.fresh("l"), scope: "local", type: "num", mutable: false };
      return pick([
        () => `queueMicrotask(() => ${later(s)});`,
        () => `process.nextTick(() => ${later(s)});`,
        () => `setTimeout(() => ${later(s)}, 0);`,
        () => `Promise.resolve(${N(s, 1)}).then((${local(x.name)}) => ${later(s, [x])});`,
        () => `((w, v) => { w.promise.then(() => ${later(s)}); w.resolve(v); })(Promise.withResolvers(), ${N(s, 1)});`,
        () => `Promise.reject(${N(s, 1)}).catch(() => ${later(s)});`,
      ])();
    },
  });

  // ---- standard functions that have a row and are called nowhere else
  num.push({
    w: 2,
    make: (g, s, d) =>
      pick([
        `new Intl.NumberFormat("en-US").format(${N(s, d)}).length`,
        `new Intl.Collator("en").compare(${S(s, d)}, ${S(s, d)})`,
        'new Intl.DateTimeFormat("en-US", { timeZone: "UTC" }).format(0).length',
        `Number(BigInt(${N(s, d)} | 0) * 3n + 2n)`,
        "Number(2n ** 10n)",
        `Number(BigInt.asUintN(8, BigInt(${N(s, d)} | 0)))`,
        `${S(s, d)}.lastIndexOf("a")`,
        `Number.parseFloat(${S(s, d)})`,
        `${S(s, d)}.search(/b/)`,
      ]),
  });
  str.push({
    w: 2,
    make: (g, s, d) =>
      pick([
        `encodeURIComponent(${S(s, d)}.toWellFormed())`, // a lone surrogate (a cut of an emoji) is a URIError
        `decodeURIComponent(encodeURIComponent(${S(s, d)}.toWellFormed()))`,
        `encodeURI(${S(s, d)}.toWellFormed())`,
        `decodeURI(encodeURI(${S(s, d)}.toWellFormed()))`,
        `String.fromCodePoint(Math.abs(${N(s, d)} | 0) % 1114112)`,
        `(Symbol.keyFor(Symbol.for("k" + ${N(s, d)})) ?? "")`,
        `new TypeError(${S(s, d)}).message`,
        `String(new RangeError("r"))`,
        `${S(s, d)}.trimEnd()`,
        `${S(s, d)}.toLocaleLowerCase()`,
        `${S(s, d)}.valueOf()`,
        `(${S(s, d)}.match(/a/g)?.join("") ?? "")`,
        `[...${S(s, d)}.matchAll(/a/g)].join("")`,
        `String(Boolean(${N(s, d)}))`,
        `(new RegExp("a+", "g").exec(${S(s, d)})?.[0] ?? "")`,
      ]),
  });
  // copies of an array, which the analysis may or may not know are new
  g.makers.push({
    name: "array copy",
    w: 2,
    ok: (g) => g.arrays(g.topScope(), "num").length > 0,
    make: (g) => {
      const s = g.topScope();
      const a = g.ref(pick(g.arrays(s, "num")));
      const text = pick([
        `${a}.toSorted()`,
        `${a}.toReversed()`,
        `${a}.with(0, ${N(s, 1)})`,
        `${a}.toSpliced(0, 0, ${N(s, 1)})`,
        `${a}.flat()`,
        `${a}.concat(${a})`,
        `${a}.slice(-2)`,
      ]);
      const b = g.binding("a", { t: "arr", elem: "num" }, { kind: "const" });
      g.add("array copy", `const ${g.ref(b)} = ${text};`, [b]);
    },
  });
  // what `console` prints besides `log`
  st.push({
    w: 2,
    make: (g, s) =>
      pick([
        `console.info("i", ${clamp(S(s, 1))});`,
        `console.debug("d", String(${N(s, 1)}));`,
        `console.table([[${N(s, 1)}, ${N(s, 1)}]]);`,
        `console.dir({ a: ${N(s, 1)} });`,
        `console.count("c");`,
      ]),
  });
}
