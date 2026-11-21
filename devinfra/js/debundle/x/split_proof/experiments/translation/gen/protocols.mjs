// Family `protocols`: what the language runs on its own, with the program's code. An object literal can carry `valueOf`,
// `toString`, `Symbol.toPrimitive`, `toJSON`, `Symbol.iterator` (a generator or a hand-written iterator with `return`),
// getters and setters, generator, async and computed methods, and a prototype (`__proto__:`); an expression that coerces,
// spreads, iterates, destructures or stringifies such an object calls them, and what they write is what the expression
// writes. A `Proxy` runs a trap on every access, and the traps count in an object that observers print. `Object` and
// `Reflect` read and change the objects, and `Object.defineProperty` adds an accessor to one that already exists.
// Every call that the language makes is recorded where the code that makes it is: `hooks` of the object type say what each
// protocol writes, and a use is not generated where a name it writes must stay.
//
// The members of the objects follow the model of `classes` (`type.cls` holds `methods` and `accessors`), so that the
// productions of that family call them too when it is installed.
import { kindOf, local } from "./core.mjs";

const PRIMS = ["num", "str", "bool"];

export function install(g) {
  const pick = (xs) => g.pick(xs);
  const N = (s, d = 2) => g.num(s, d);
  const S = (s, d = 2) => g.str(s, d);
  const clamp = (text) => `(${text}).slice(0, 24)`;
  const usable = (s, writes) => ![...writes].some((n) => s.frozen.has(n));
  const take = (s, writes) => {
    for (const n of writes) s.writes.add(n);
  };
  const named = (type) => ({ name: g.fresh("l"), scope: "local", type, mutable: false });
  /** A scope for the body of a hook or a method. `ret: "object"` has no `return` statement: the tail returns. */
  const scopeFor = (ps = [], fn = {}) => ({
    ...g.topScope(),
    locals: ps,
    top: false,
    inFunction: true,
    fn: { ret: "object", async: false, generator: false, arguments: true, ...fn },
    nest: 2,
  });
  const block = (s, tail, n = 1 + g.int(2)) => `{ ${[...Array.from({ length: n }, () => g.stmt(s)), tail].join(" ")} }`;
  const objects = (s, ok = () => true) => g.refs(s, (b) => kindOf(b.type) === "obj" && !s.frozen.has(b.name) && ok(b));
  const counters = (s) => g.refs(s, (b) => kindOf(b.type) === "obj" && b.type.counter);

  // ---- object literals with hooks, accessors and methods
  g.makers.push({
    name: "object with protocols",
    w: 6,
    make: (g) => {
      const top = g.topScope();
      const hooks = Object.create(null); // what each protocol the language calls writes: valueOf, toString, toPrimitive, iterator, toJSON
      const cls = { methods: {}, accessors: {}, peeks: [] };
      const fields = { v: "num" };
      const parts = [`v: ${N(top, 1)}`];
      const bases = g.refs(
        top,
        (b) =>
          kindOf(b.type) === "obj" && !b.type.hooks && !b.type.bare && !b.type.opaque && !b.type.proxy && !b.type.fixed // a frozen base: assigning to a property that is inherited and read-only throws
      );
      const base = bases.length > 0 && g.chance(0.2) ? pick(bases) : null;
      if (base) parts.unshift(`__proto__: ${g.ref(base)}`);
      if (g.chance(0.5)) {
        const s = scopeFor([], { ret: "num" });
        hooks.valueOf = s.writes;
        parts.push(`valueOf() ${block(s, `return this.v + ${N(s, 1)};`)}`);
      }
      if (g.chance(0.4)) {
        const s = scopeFor();
        hooks.toString = s.writes;
        parts.push(`toString() ${block(s, `return ${clamp(`String(this.v) + ${S(s, 1)}`)};`)}`);
      }
      if (g.chance(0.35)) {
        const hint = named("str");
        const s = scopeFor([hint]);
        hooks.toPrimitive = s.writes;
        parts.push(
          `[Symbol.toPrimitive](${local(hint.name)}) ${block(s, `return ${local(hint.name)} === "string" ? ${clamp(S(s, 1))} : ${N(s, 1)};`)}`
        );
      }
      if (g.chance(0.45)) {
        if (g.chance(0.5)) {
          const s = scopeFor([], { generator: true });
          hooks.iterator = s.writes;
          parts.push(`*[Symbol.iterator]() ${block(s, `yield ${N(s, 1)}; yield this.v;`, g.int(2))}`);
        } else {
          // a hand-written iterator: `next` counts to two, and `return` runs when a consumer stops early
          // `next` and `return` are two functions: what one declares the other does not see
          const next = scopeFor();
          const done = scopeFor();
          hooks.iterator = new Set([...next.writes, ...done.writes]);
          const value = N(next, 1);
          const stop = g.chance(0.4) ? `, return: () => { ${g.stmt(done)} return { done: true }; }` : "";
          for (const n of done.writes) hooks.iterator.add(n);
          parts.push(
            `[Symbol.iterator]() { let i = 0; return { next: () => (i++ < 2 ? { value: ${value}, done: false } : { value: undefined, done: true })${stop} }; }`
          );
          for (const n of next.writes) hooks.iterator.add(n);
        }
      }
      if (g.chance(0.3)) {
        const s = scopeFor();
        hooks.toJSON = s.writes;
        parts.push(`toJSON() ${block(s, `return ${N(s, 1)};`)}`);
      }
      // accessors: a getter, a pair, a setter alone
      if (g.chance(0.5)) {
        const name = g.fresh("a");
        const form = pick(["get", "pair", "set"]);
        const get = form !== "set" ? scopeFor() : null;
        const p = named("num");
        const set = form !== "get" ? scopeFor([p]) : null;
        if (get) parts.push(`get ${name}() ${block(get, `return ${N(get, 1)};`)}`);
        if (set) parts.push(`set ${name}(${local(p.name)}) ${block(set, `this.v = ${local(p.name)};`)}`);
        cls.accessors[name] = { get: get?.writes ?? null, set: set?.writes ?? null };
      }
      // methods: plain, computed, generator, async
      for (let i = 0, n = g.int(3); i < n; i++) {
        const name = g.fresh("m");
        const kind = pick(["method", "computed", "gen", "async"]);
        const p = named("num");
        const s = scopeFor([p], { generator: kind === "gen", async: kind === "async" });
        if (kind === "async") g.late = true;
        const key = kind === "computed" ? `["m" + ${name.slice(1)}]` : name;
        const star = kind === "gen" ? "*" : "";
        parts.push(
          `${kind === "async" ? "async " : ""}${star}${key}(${local(p.name)}) ${block(s, `return ${N(s, 1)};`)}`
        );
        cls.methods[name] = { kind: kind === "computed" ? "method" : kind, n: 1, writes: s.writes };
      }
      if (base) for (const [f, t] of Object.entries(base.type.fields)) fields[f] ??= t;
      const o = g.binding("o", { t: "obj", fields, cls, hooks, opaque: base !== null }, { kind: "const" });
      g.add("object with protocols", `const ${g.ref(o)} = { ${parts.join(", ")} };`, [o]);
    },
  });

  // ---- what the language calls: coercion, `JSON.stringify`, iteration, destructuring, spread
  const hooked = (s) => objects(s, (b) => b.type.hooks);
  const numHook = (h) => h.toPrimitive ?? h.valueOf ?? h.toString ?? null;
  const strHook = (h) => h.toPrimitive ?? h.toString ?? null;
  const jsonHook = (h, cls) => {
    if (h.toJSON) return h.toJSON;
    const writes = new Set();
    for (const a of Object.values(cls.accessors)) if (a.get) for (const n of a.get) writes.add(n);
    return writes;
  };
  /** The hooked objects whose call (by `which`) may run here. */
  const ready = (s, which) =>
    hooked(s).filter((o) => usable(s, which(o.type.hooks, o.type.cls) ?? []) && which(o.type.hooks, o.type.cls));
  const use = (s, o, writes) => {
    g.touch(s, o);
    take(s, writes);
    return g.ref(o);
  };
  const num = g.prods.num;
  num.push({
    w: 3,
    ok: (g, s) => ready(s, numHook).length > 0,
    make: (g, s, d) => {
      const o = pick(ready(s, numHook));
      const x = use(s, o, numHook(o.type.hooks));
      return pick([
        `(+${x})`,
        `(${x} * ${N(s, d)})`,
        `(${N(s, d)} - ${x})`,
        `Number(${x})`,
        `Math.max(${x}, ${N(s, d)})`,
      ]);
    },
  });
  g.prods.str.push({
    w: 3,
    ok: (g, s) => ready(s, strHook).length > 0 || ready(s, numHook).length > 0 || ready(s, jsonHook).length > 0,
    make: (g, s) => {
      const forms = [];
      const a = ready(s, strHook);
      if (a.length > 0) {
        const o = pick(a);
        const x = use(s, o, strHook(o.type.hooks));
        forms.push(`String(${x})`, `\`\${${x}}\``);
      }
      const b = ready(s, numHook);
      if (b.length > 0) {
        const o = pick(b);
        forms.push(`(${use(s, o, numHook(o.type.hooks))} + "")`);
      }
      const c = ready(s, jsonHook);
      if (c.length > 0) {
        const o = pick(c);
        forms.push(`JSON.stringify(${use(s, o, jsonHook(o.type.hooks, o.type.cls))})`);
      }
      return pick(forms);
    },
  });
  g.prods.bool.push({
    w: 2,
    ok: (g, s) => ready(s, numHook).length > 0,
    make: (g, s, d) => {
      const o = pick(ready(s, numHook));
      const x = use(s, o, numHook(o.type.hooks));
      return `(${x} ${pick(["<", "<=", ">", "==", "!="])} ${N(s, d)})`;
    },
  });
  const iterHook = (h) => h.iterator ?? null;
  num.push({
    w: 3,
    ok: (g, s) => ready(s, iterHook).length > 0,
    make: (g, s, d) => {
      const o = pick(ready(s, iterHook));
      const x = use(s, o, o.type.hooks.iterator);
      return pick([
        `[...${x}].length`,
        `Array.from(${x}).length`,
        `new Set(${x}).size`,
        `Math.max(...${x}, ${N(s, d)})`,
        `[...${x}].reduce((p, q) => p + q, 0)`,
        `Array.from(${x}, (y) => y * 2).length`,
      ]);
    },
  });
  g.prods.str.push({
    w: 1,
    ok: (g, s) => ready(s, iterHook).length > 0,
    make: (g, s) => {
      const o = pick(ready(s, iterHook));
      return `[...${use(s, o, o.type.hooks.iterator)}].join(",")`;
    },
  });
  const st = g.stmts;
  st.push({
    w: 3,
    ok: (g, s) => s.nest > 0 && ready(s, iterHook).length > 0,
    make: (g, s) => {
      const o = pick(ready(s, iterHook));
      const x = use(s, o, o.type.hooks.iterator);
      const item = named("num");
      const sc = g.child(s, { locals: [...s.locals, item], loops: s.loops + 1, breakable: s.breakable + 1 });
      return `for (const ${local(item.name)} of ${x}) ${g.block(sc)}`;
    },
  });
  st.push({
    w: 2,
    ok: (g, s) => s.fn?.generator && !s.noJump && ready(s, iterHook).length > 0,
    make: (g, s) => {
      const o = pick(ready(s, iterHook));
      return `yield* ${use(s, o, o.type.hooks.iterator)};`;
    },
  });
  // a pattern over an iterable takes what it needs and, for a hand-written iterator, calls `return`
  st.push({
    w: 2,
    ok: (g, s) => !s.top && ready(s, iterHook).length > 0,
    make: (g, s) => {
      const o = pick(ready(s, iterHook));
      const x = use(s, o, o.type.hooks.iterator);
      const a = named("num");
      const b = named("num");
      const text = `const [${local(a.name)} = 0, ${local(b.name)} = 1] = ${x};`;
      s.locals.push(a, b);
      return text;
    },
  });
  g.makers.push({
    name: "destructuring of an iterable",
    w: 2,
    ok: (g) => ready(g.topScope(), iterHook).length > 0,
    make: (g) => {
      const s = g.topScope();
      const o = pick(ready(s, iterHook));
      const a = g.binding("v", "num", { kind: "const" });
      const b = g.binding("v", "num", { kind: "const" });
      g.add("destructuring of an iterable", `const [${g.ref(a)} = 0, ${g.ref(b)} = 1] = ${g.ref(o)};`, [a, b]);
    },
  });

  // ---- a Proxy: every access runs a trap, and the traps count in an object that observers print
  g.makers.push({
    name: "counter",
    w: 2,
    ok: (g) => counters(g.topScope()).length === 0,
    make: (g) => {
      const c = g.binding("o", { t: "obj", fields: { n: "num" }, counter: true }, { kind: "const" });
      g.add("counter", `const ${g.ref(c)} = { n: 0 };`, [c]);
    },
  });
  const TRAPS = [
    (c) => `get(target, key, receiver) { ${c}.n += 1; return Reflect.get(target, key, receiver); }`,
    (c) => `set(target, key, value, receiver) { ${c}.n += 2; return Reflect.set(target, key, value, receiver); }`,
    (c) => `has(target, key) { ${c}.n += 3; return Reflect.has(target, key); }`,
    (c) => `deleteProperty(target, key) { ${c}.n += 4; return Reflect.deleteProperty(target, key); }`,
    (c) => `ownKeys(target) { ${c}.n += 5; return Reflect.ownKeys(target); }`,
    (c) =>
      `getOwnPropertyDescriptor(target, key) { ${c}.n += 6; return Reflect.getOwnPropertyDescriptor(target, key); }`,
    (c) =>
      `defineProperty(target, key, descriptor) { ${c}.n += 7; return Reflect.defineProperty(target, key, descriptor); }`,
    (c) => `getPrototypeOf(target) { ${c}.n += 8; return Reflect.getPrototypeOf(target); }`,
  ];
  const plainObjects = (s) =>
    objects(
      s,
      (b) =>
        !b.type.hooks &&
        !b.type.fixed &&
        !b.type.sealed &&
        !b.type.bare &&
        !b.type.opaque &&
        !b.type.proxy &&
        !b.type.counter
    );
  g.makers.push({
    name: "proxy",
    w: 4,
    ok: (g) => !g.avoid.has("proxy") && counters(g.topScope()).length > 0 && plainObjects(g.topScope()).length > 0,
    make: (g) => {
      const s = g.topScope();
      const target = pick(plainObjects(s));
      const counter = pick(counters(s));
      const chosen = TRAPS.filter(() => g.chance(0.5));
      if (chosen.length === 0) chosen.push(TRAPS[0], TRAPS[1]);
      const p = g.binding(
        "o",
        { t: "obj", fields: { ...target.type.fields }, opaque: true, proxy: true },
        { kind: "const" }
      );
      g.add(
        "proxy",
        `const ${g.ref(p)} = new Proxy(${g.ref(target)}, { ${chosen.map((trap) => trap(g.ref(counter))).join(", ")} });`,
        [p]
      );
    },
  });
  // a function behind a Proxy: the `apply` trap counts, and the function's callers count with it
  const plainFunctions = (s) =>
    g.refs(
      s,
      (b) =>
        kindOf(b.type) === "fn" &&
        b.type.ret === "num" &&
        !b.type.async &&
        !b.type.thisField &&
        !b.type.tag &&
        !b.type.gen
    );
  g.makers.push({
    name: "proxy of a function",
    w: 2,
    ok: (g) => counters(g.topScope()).length > 0 && plainFunctions(g.topScope()).length > 0,
    make: (g) => {
      const s = g.topScope();
      const f = pick(plainFunctions(s));
      const counter = pick(counters(s));
      const p = g.binding("f", null, { kind: "const" });
      p.type = { t: "fn", n: f.type.n, ret: "num", writes: new Set([...f.type.writes, counter.name]) };
      g.add(
        "proxy of a function",
        `const ${g.ref(p)} = new Proxy(${g.ref(f)}, { apply(target, thisArg, args) { ${g.ref(counter)}.n += 9; return Reflect.apply(target, thisArg, args); } });`,
        [p]
      );
    },
  });
  // `Object.defineProperty` adds an accessor that counts to an object, which the generic productions then read and write
  st.push({
    w: 2,
    ok: (g, s) => counters(s).length > 0 && plainObjects(s).length > 0,
    make: (g, s) => {
      const o = pick(plainObjects(s));
      const c = g.ref(pick(counters(s)));
      g.touch(s, o);
      const name = g.fresh("w");
      o.type.fields[name] = "num";
      o.type.opaque = true; // a copy of it has the value of the getter, not the accessor
      return `Object.defineProperty(${g.ref(o)}, "${name}", { get() { ${c}.n += 10; return ${g.int(9)}; }, set(v) { ${c}.n += 11; }, enumerable: true, configurable: true });`;
    },
  });

  // ---- Object and Reflect: queries, changes, and objects made by them
  const withField = (s, type, ok = () => true) =>
    objects(s, (b) => !b.type.hooks && !b.type.counter && Object.values(b.type.fields).includes(type) && ok(b));
  const fieldOf = (o, type) => pick(Object.entries(o.type.fields).filter(([, t]) => t === type))[0];
  const key = (f) => JSON.stringify(f);
  num.push({
    w: 3,
    ok: (g, s) => objects(s, (b) => !b.type.hooks && !b.type.counter).length > 0,
    make: (g, s, d) => {
      const o = pick(objects(s, (b) => !b.type.hooks && !b.type.counter));
      const x = g.ref(o);
      const forms = [
        `Object.keys(${x}).length`,
        `Object.values(${x}).length`,
        `Object.entries(${x}).length`,
        `Object.getOwnPropertyNames(${x}).length`,
        `Reflect.ownKeys(${x}).length`,
        `Object.getOwnPropertySymbols(${x}).length`,
      ];
      if (Object.values(o.type.fields).includes("num")) {
        const f = fieldOf(o, "num");
        forms.push(
          `(Reflect.get(${x}, ${key(f)}) ?? ${N(s, d)})`,
          `(Object.getOwnPropertyDescriptor(${x}, ${key(f)})?.value ?? ${N(s, d)})`
        );
      }
      return pick(forms);
    },
  });
  g.prods.bool.push({
    w: 3,
    ok: (g, s) => objects(s, (b) => !b.type.hooks && !b.type.counter).length > 0,
    make: (g, s) => {
      const o = pick(objects(s, (b) => !b.type.hooks && !b.type.counter));
      const x = g.ref(o);
      const f = key(pick(Object.keys(o.type.fields)));
      return pick([
        `Reflect.has(${x}, ${f})`,
        `Object.hasOwn(${x}, ${f})`,
        `Object.isFrozen(${x})`,
        `Object.isSealed(${x})`,
        `Object.isExtensible(${x})`,
        `Reflect.isExtensible(${x})`,
        `(Reflect.getPrototypeOf(${x}) === Object.prototype)`,
        `(Object.getPrototypeOf(${x}) === null)`,
        `Object.prototype.propertyIsEnumerable.call(${x}, ${f})`,
        `Object.prototype.isPrototypeOf.call(Object.prototype, ${x})`,
        `(Object.prototype.toString.call(${x}) === "[object Object]")`,
      ]);
    },
  });
  st.push({
    w: 4,
    ok: (g, s) => withField(s, "num", (b) => !b.type.fixed && !b.type.bare).length > 0,
    make: (g, s) => {
      const o = pick(withField(s, "num", (b) => !b.type.fixed && !b.type.bare));
      g.touch(s, o);
      const x = g.ref(o);
      const f = fieldOf(o, "num");
      const descriptor = `{ value: ${N(s, 1)}, writable: true, enumerable: true, configurable: true }`;
      // a sealed object keeps its properties as they are: they cannot be defined again, or deleted
      return pick([
        `Reflect.set(${x}, ${key(f)}, ${N(s, 1)});`,
        `Object.assign(${x}, { [${key(f)}]: ${N(s, 1)} });`,
        ...(o.type.sealed
          ? []
          : [
              `Reflect.defineProperty(${x}, ${key(f)}, ${descriptor});`,
              `Object.defineProperty(${x}, ${key(f)}, ${descriptor});`,
              `Object.defineProperties(${x}, { [${key(f)}]: ${descriptor} });`,
              `Reflect.deleteProperty(${x}, ${key(f)});`,
            ]),
      ]);
    },
  });
  // objects that are frozen, sealed, non-extensible, made with a prototype or without one, or copied
  g.makers.push({
    name: "frozen object",
    w: 2,
    make: (g) => {
      const s = g.topScope();
      const how = pick(["freeze", "seal", "preventExtensions"]);
      const fields = { a: "num", b: "str" };
      // a non-extensible object takes no new field, which is what `sealed` stops the other productions from adding
      const o = g.binding("o", { t: "obj", fields, fixed: how === "freeze", sealed: true }, { kind: "const" });
      g.add("frozen object", `const ${g.ref(o)} = Object.${how}({ a: ${N(s, 1)}, b: ${clamp(S(s, 1))} });`, [o]);
    },
  });
  g.makers.push({
    name: "object made by Object",
    w: 3,
    make: (g) => {
      const s = g.topScope();
      const bases = plainObjects(s);
      const form = g.int(4);
      if (form === 0 && bases.length > 0) {
        const base = pick(bases);
        const o = g.binding(
          "o",
          { t: "obj", fields: { ...base.type.fields, x: "num" }, opaque: true },
          { kind: "const" }
        );
        g.add(
          "object made by Object",
          `const ${g.ref(o)} = Object.create(${g.ref(base)}, { x: { value: ${N(s, 1)}, writable: true, enumerable: true, configurable: true } });`,
          [o]
        );
      } else if (form === 1) {
        const o = g.binding("o", { t: "obj", fields: { a: "num" }, bare: true }, { kind: "const" });
        g.add("object made by Object", `const ${g.ref(o)} = Object.assign(Object.create(null), { a: ${N(s, 1)} });`, [
          o,
        ]);
      } else if (form === 2) {
        const o = g.binding("o", { t: "obj", fields: { a: "num", b: "num" } }, { kind: "const" });
        g.add(
          "object made by Object",
          `const ${g.ref(o)} = Object.fromEntries([["a", ${N(s, 1)}], ["b", ${N(s, 1)}]]);`,
          [o]
        );
      } else {
        // `structuredClone` of an object that holds a function throws, and an object made by a spread may hold one that its type
        // does not say, so it takes a literal
        if (g.chance(0.4)) {
          const o = g.binding("o", { t: "obj", fields: { a: "num", b: "str" } }, { kind: "const" });
          g.add(
            "object made by Object",
            `const ${g.ref(o)} = structuredClone({ a: ${N(s, 1)}, b: ${clamp(S(s, 1))} });`,
            [o]
          );
          return;
        }
        const sources = objects(
          s,
          (b) =>
            !b.type.hooks &&
            !b.type.opaque &&
            !b.type.bare &&
            !b.type.counter &&
            Object.values(b.type.fields).every((t) => PRIMS.includes(t))
        );
        if (sources.length === 0) return;
        const from = pick(sources);
        const o = g.binding("o", { t: "obj", fields: { ...from.type.fields } }, { kind: "const" });
        g.add(
          "object made by Object",
          `const ${g.ref(o)} = ${pick([`Object.fromEntries(Object.entries(${g.ref(from)}))`, `Object.assign({}, ${g.ref(from)})`])};`,
          [o]
        );
      }
    },
  });
  // `yield` with no argument yields `undefined`, which the consumers above take as `NaN` or skip
  st.push({ w: 1, ok: (g, s) => s.fn?.generator && !s.noJump, make: () => "yield;" });
  // shorthand properties: the names are the field names, so they are plain; the block is the scope of both
  st.push({
    w: 2,
    ok: (g, s) => !s.top && s.nest > 0,
    make: (g, s) => {
      const o = {
        name: g.fresh("l"),
        scope: "local",
        type: { t: "obj", fields: { a: "num", b: "str" } },
        mutable: false,
      };
      const sc = g.child(s, { locals: [...s.locals, o], nest: s.nest - 1 });
      const [first, second] = [N(s, 1), clamp(S(s, 1))];
      return `{ const a = ${first}, b = ${second}; const ${local(o.name)} = { a, b }; ${g.stmt(sc)} ${g.stmt(sc)} }`;
    },
  });
}
