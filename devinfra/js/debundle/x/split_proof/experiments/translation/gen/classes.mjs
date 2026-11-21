// Family `classes`: class declarations and expressions (named, anonymous, local to a block) with instance, private, static
// and computed fields, arrow-function fields, methods (plain, private, static, generator, async, computed), accessors (also
// private and static), static blocks, `extends` (an identifier, an expression, `null`), `super(…)`, `super.m(…)`,
// `new.target`, `#x in o` and `instanceof`. A class is a binding of kind `cls`; an instance is an `obj` binding with the
// public fields of its class, so that every production written for objects applies to it too. Everything a method,
// accessor or constructor may change is recorded (`writes`), as for a function. A member calls only members generated
// before it, so there is no recursion, and nothing overrides a method, so `this.m()` is the method that was written.
import { kindOf, local } from "./core.mjs";

const FIELDS = ["f", "g", "h", "k"]; // public instance fields: a class and its parents declare each of them at most once
const PRIMS = ["num", "str", "bool"];

export function install(g) {
  const pick = (xs) => g.pick(xs);
  const N = (s, d = 2) => g.num(s, d);
  const numeric = (name) => ({ name, scope: "local", type: "num", mutable: true });
  const names = (ps) => ps.map((p) => local(p.name)).join(", ");
  const argList = (s, d, n) => Array.from({ length: n }, () => N(s, d)).join(", ");
  const classes = (s) => g.refs(s, (b) => kindOf(b.type) === "cls");
  const usable = (s, writes) => ![...writes].some((n) => s.frozen.has(n));
  const merge = (into, from) => {
    for (const n of from) into.add(n);
  };
  /** What the callee changes, the caller changes. */
  const take = (s, writes) => merge(s.writes, writes);
  /** The entries of a table of members that may be called here: not where they would change a name that must stay. */
  const callable = (s, table, kinds = ["method"]) =>
    Object.entries(table).filter(([, info]) => kinds.includes(info.kind) && usable(s, info.writes));
  /** `super.m` finds the methods on the prototype, not the arrow functions that a field holds on each instance. */
  const onPrototype = (s, table) => callable(s, table).filter(([, info]) => !info.field);
  const readable = (s, table) => Object.entries(table).filter(([, a]) => a.get && usable(s, a.get));
  const settable = (s, table) => Object.entries(table).filter(([, a]) => a.set && usable(s, a.set));
  const instances = (s) => g.refs(s, (b) => kindOf(b.type) === "obj" && b.type.cls && !s.frozen.has(b.name));
  const constructible = (s) => classes(s).filter((c) => c.type.ctor && usable(s, c.type.ctor.writes));
  /** `this` is an instance of the class being written: not in a static member, not before `super(…)` returns. */
  const here = (s) => s.self && !s.self.staticSide && !s.self.beforeSuper;
  const invoke = (s, d, head, info) => {
    take(s, info.writes);
    return `${head}(${argList(s, d, info.n)})`;
  };

  // ---- expressions
  const num = g.prods.num;
  // inside a member: `this`, `super`, and the statics of the class, named inside its body (a class expression without a name
  // has no way to name itself)
  const ownFields = (s) => [
    ...Object.keys(s.self.cls.fields).map((f) => `this.${f}`),
    ...s.self.privateFields.map((p) => `this.#${p}`),
  ];
  num.push({ w: 6, leaf: true, ok: (g, s) => here(s) && ownFields(s).length > 0, make: (g, s) => pick(ownFields(s)) });
  num.push({
    w: 2,
    leaf: true,
    ok: (g, s) => here(s) && Object.values(s.self.privateGetters).some((w) => usable(s, w)),
    make: (g, s) => {
      const [p, writes] = pick(Object.entries(s.self.privateGetters).filter(([, w]) => usable(s, w)));
      take(s, writes);
      return `this.#${p}`;
    },
  });
  num.push({
    w: 4,
    ok: (g, s) => here(s) && callable(s, s.self.cls.methods).length > 0,
    make: (g, s, d) => {
      const [m, info] = pick(callable(s, s.self.cls.methods));
      return invoke(s, d, `this.${m}`, info);
    },
  });
  num.push({
    w: 2,
    ok: (g, s) => here(s) && callable(s, s.self.privateMethods).length > 0,
    make: (g, s, d) => {
      const [m, info] = pick(callable(s, s.self.privateMethods));
      return invoke(s, d, `this.#${m}`, info);
    },
  });
  num.push({
    w: 2,
    ok: (g, s) => here(s) && s.self.parentType && onPrototype(s, s.self.parentType.methods).length > 0,
    make: (g, s, d) => {
      const [m, info] = pick(onPrototype(s, s.self.parentType.methods));
      return invoke(s, d, `super.${m}`, info);
    },
  });
  num.push({
    w: 2,
    leaf: true,
    ok: (g, s) => here(s) && readable(s, s.self.cls.accessors).length > 0,
    make: (g, s) => {
      const [a, info] = pick(readable(s, s.self.cls.accessors));
      take(s, info.get);
      return pick([`this.${a}`, ...(s.self.parentType?.accessors[a]?.get ? [`super.${a}`] : [])]);
    },
  });
  const ownStatics = (s) => [
    ...Object.keys(s.self.cls.statics).map((f) => `${s.self.me}.${f}`),
    ...s.self.staticPrivateFields.map((p) => `${s.self.me}.#${p}`),
  ];
  num.push({
    w: 3,
    leaf: true,
    ok: (g, s) => s.self?.me && ownStatics(s).length > 0,
    make: (g, s) => pick(ownStatics(s)),
  });
  const ownStaticCalls = (s) => [
    ...callable(s, s.self.cls.staticMethods).map(([m, info]) => [`${s.self.me}.${m}`, info]),
    ...callable(s, s.self.staticPrivateMethods).map(([m, info]) => [`${s.self.me}.#${m}`, info]),
  ];
  num.push({
    w: 2,
    ok: (g, s) => s.self?.me && ownStaticCalls(s).length > 0,
    make: (g, s, d) => {
      const [head, info] = pick(ownStaticCalls(s));
      return invoke(s, d, head, info);
    },
  });
  // outside: the statics of a class, `new C(…).f`, and the methods and accessors of an instance
  const staticsOf = (s) => classes(s).flatMap((c) => Object.keys(c.type.statics).map((f) => [c, f]));
  num.push({
    w: 3,
    leaf: true,
    ok: (g, s) => staticsOf(s).length > 0,
    make: (g, s) => {
      const [c, f] = pick(staticsOf(s));
      return `${g.ref(c)}.${f}`;
    },
  });
  num.push({
    w: 2,
    ok: (g, s) => classes(s).some((c) => callable(s, c.type.staticMethods).length > 0),
    make: (g, s, d) => {
      const c = pick(classes(s).filter((c) => callable(s, c.type.staticMethods).length > 0));
      const [m, info] = pick(callable(s, c.type.staticMethods));
      g.touch(s, c);
      return invoke(s, d, `${g.ref(c)}.${m}`, info);
    },
  });
  num.push({
    w: 2,
    leaf: true,
    ok: (g, s) => classes(s).some((c) => readable(s, c.type.staticAccessors).length > 0),
    make: (g, s) => {
      const c = pick(classes(s).filter((c) => readable(s, c.type.staticAccessors).length > 0));
      const [a, info] = pick(readable(s, c.type.staticAccessors));
      g.touch(s, c);
      take(s, info.get);
      return `${g.ref(c)}.${a}`;
    },
  });
  num.push({
    w: 2,
    ok: (g, s) => constructible(s).some((c) => Object.keys(c.type.fields).length > 0),
    make: (g, s, d) => {
      const c = pick(constructible(s).filter((c) => Object.keys(c.type.fields).length > 0));
      take(s, c.type.ctor.writes);
      return `new ${g.ref(c)}(${argList(s, d, c.type.ctor.n)}).${pick(Object.keys(c.type.fields))}`;
    },
  });
  const withMembers = (s, kinds) => instances(s).filter((o) => callable(s, o.type.cls.methods, kinds).length > 0);
  const callOn = (s, d, kinds) => {
    const o = pick(withMembers(s, kinds));
    const [m, info] = pick(callable(s, o.type.cls.methods, kinds));
    g.touch(s, o);
    return invoke(s, d, `${g.ref(o)}.${m}`, info);
  };
  num.push({ w: 4, ok: (g, s) => withMembers(s, ["method"]).length > 0, make: (g, s, d) => callOn(s, d, ["method"]) });
  const withGetters = (s) => instances(s).filter((o) => readable(s, o.type.cls.accessors).length > 0);
  num.push({
    w: 2,
    leaf: true,
    ok: (g, s) => withGetters(s).length > 0,
    make: (g, s) => {
      const o = pick(withGetters(s));
      const [a, info] = pick(readable(s, o.type.cls.accessors));
      g.touch(s, o);
      take(s, info.get);
      return `${g.ref(o)}.${a}`;
    },
  });
  // a generator method, consumed; an async method, awaited
  num.push({
    w: 3,
    ok: (g, s) => !s.fn?.generator && withMembers(s, ["gen"]).length > 0,
    make: (g, s, d) => {
      const it = callOn(s, d, ["gen"]);
      return pick([
        `[...${it}].length`,
        `Array.from(${it}).length`,
        `(${it}.next().value ?? 0)`,
        `new Set(${it}).size`,
      ]);
    },
  });
  g.prods.str.push({
    w: 2,
    ok: (g, s) => !s.fn?.generator && withMembers(s, ["gen"]).length > 0,
    make: (g, s, d) => `[...${callOn(s, d, ["gen"])}].join(",")`,
  });
  num.push({
    w: 3,
    ok: (g, s) => s.fn?.async && withMembers(s, ["async"]).length > 0,
    make: (g, s, d) => `(await ${callOn(s, d, ["async"])})`,
  });
  g.prods.bool.push({
    w: 2,
    ok: (g, s) => instances(s).length > 0 && classes(s).length > 0,
    make: (g, s) => `(${g.ref(pick(instances(s)))} instanceof ${g.ref(pick(classes(s)))})`,
  });
  // `#x in o`: whether an object was made by the class that declares `#x`
  g.prods.bool.push({
    w: 2,
    ok: (g, s) => s.self?.privateFields.length > 0,
    make: (g, s) => {
      const others = g.refs(s, (b) => kindOf(b.type) === "obj");
      const subject =
        here(s) && (others.length === 0 || g.chance(0.5)) ? "this" : others.length > 0 ? g.ref(pick(others)) : null;
      return subject ? `(#${pick(s.self.privateFields)} in ${subject})` : "true";
    },
  });

  // ---- statements
  const st = g.stmts;
  let depth = 0; // classes being written: a body in a member may hold a class, and a body in that one may not
  st.push({
    w: 6,
    ok: (g, s) => here(s) && ownFields(s).length > 0,
    make: (g, s) => {
      const place = pick(ownFields(s));
      return pick([
        `${place} = ${N(s)};`, `${place} += ${N(s, 1)};`, `${place}++;`, `--${place};`, `${place} ||= ${N(s, 1)};`,
        `${place} ??= ${N(s, 1)};`, `${place} *= ${N(s, 1)};`,
      ]); // prettier-ignore
    },
  });
  st.push({
    w: 2,
    ok: (g, s) => here(s) && settable(s, s.self.cls.accessors).length > 0,
    make: (g, s) => {
      const [a, info] = pick(settable(s, s.self.cls.accessors));
      take(s, info.set);
      return `this.${a} = ${N(s, 1)};`;
    },
  });
  st.push({
    w: 4,
    ok: (g, s) => s.self?.me && ownStatics(s).length > 0,
    make: (g, s) => {
      const place = pick(ownStatics(s));
      g.touch(s, s.self.binding);
      return pick([`${place} = ${N(s)};`, `${place} += ${N(s, 1)};`, `${place}++;`, `${place} ??= ${N(s, 1)};`]);
    },
  });
  st.push({
    w: 3,
    ok: (g, s) => staticsOf(s).length > 0,
    make: (g, s) => {
      const [c, f] = pick(staticsOf(s));
      g.touch(s, c);
      const place = `${g.ref(c)}.${f}`;
      return pick([`${place} = ${N(s)};`, `${place} += ${N(s, 1)};`, `${place}++;`, `${place} ||= ${N(s, 1)};`]);
    },
  });
  st.push({
    w: 1,
    ok: (g, s) => classes(s).some((c) => settable(s, c.type.staticAccessors).length > 0),
    make: (g, s) => {
      const c = pick(classes(s).filter((c) => settable(s, c.type.staticAccessors).length > 0));
      const [a, info] = pick(settable(s, c.type.staticAccessors));
      g.touch(s, c);
      take(s, info.set);
      return `${g.ref(c)}.${a} = ${N(s, 1)};`;
    },
  });
  const withSetters = (s) => instances(s).filter((o) => settable(s, o.type.cls.accessors).length > 0);
  st.push({
    w: 2,
    ok: (g, s) => withSetters(s).length > 0,
    make: (g, s) => {
      const o = pick(withSetters(s));
      const [a, info] = pick(settable(s, o.type.cls.accessors));
      g.touch(s, o);
      take(s, info.set);
      return `${g.ref(o)}.${a} = ${N(s, 1)};`;
    },
  });
  st.push({
    w: 2,
    ok: (g, s) => constructible(s).length > 0,
    make: (g, s) => {
      const c = pick(constructible(s));
      take(s, c.type.ctor.writes);
      return `new ${g.ref(c)}(${argList(s, 1, c.type.ctor.n)});`;
    },
  });
  // an async method started, an async generator method iterated; both finish after the program
  const outsideAsync = (s) => !s.fn?.generator && !s.fn?.async;
  st.push({
    w: 2,
    ok: (g, s) => outsideAsync(s) && withMembers(s, ["async"]).length > 0,
    make: (g, s) => {
      g.late = true;
      const start = callOn(s, 1, ["async"]);
      const v = { ...numeric(g.fresh("l")), mutable: false };
      return pick([
        `${start};`,
        `void ${start};`,
        `${start}.then((${local(v.name)}) => ${g.block(g.callback(s, [v], { ret: "void" }), 1)});`,
      ]);
    },
  });
  st.push({
    w: 2,
    ok: (g, s) => outsideAsync(s) && withMembers(s, ["asyncgen"]).length > 0,
    make: (g, s) => {
      g.late = true;
      const it = callOn(s, 1, ["asyncgen"]);
      const x = { ...numeric(g.fresh("l")), mutable: false };
      const fn = { ret: "void", async: true, generator: false, arrow: true };
      const inner = g.callback(s, [x], { ret: "void", fn, loops: 1, breakable: 1 });
      return `(async () => { for await (const ${local(x.name)} of ${it}) ${g.block(inner, 1)} })();`;
    },
  });
  // a class written in a body: it sees the primitives around it, and is used in the statements after it
  st.push({
    w: 2,
    ok: (g, s) => !s.top && s.nest > 0 && depth < 2,
    make: (g, s) => {
      const made = makeClass({ outer: s });
      s.locals.push(made.binding);
      if (!made.type.ctor || !usable(s, made.type.ctor.writes)) return made.text;
      take(s, made.type.ctor.writes);
      const instance = {
        name: g.fresh("l"),
        scope: "local",
        type: { t: "obj", fields: { ...made.type.fields }, cls: made.type },
        mutable: false,
      };
      const text = `${made.text} const ${local(instance.name)} = new ${g.ref(made.binding)}(${argList(s, 1, made.type.ctor.n)});`;
      s.locals.push(instance);
      return text;
    },
  });

  // ---- the class
  /**
   * One class, as `{ text, binding, type }`. At the module scope (`outer` is `null`) `text` is a unit that declares
   * `binding`. In a body (`outer` is the scope of the body) it is a declaration statement: its members see the primitives
   * around it, which they do not change, and what they may change is added to the body's `writes`.
   */
  function makeClass(options) {
    depth++;
    try {
      return writeClass(options);
    } finally {
      depth--;
    }
  }
  function writeClass({ expression = false, outer = null }) {
    const binding = outer
      ? { name: g.fresh("L"), scope: "local", type: null, kind: "class", mutable: false }
      : g.binding("C", null, { kind: expression ? "const" : "class", mutable: false });
    const anonymous = !outer && expression && g.chance(0.3);
    const inner = !outer && expression && !anonymous ? g.fresh("l") : null;
    const me = anonymous ? null : inner ? local(inner) : g.ref(binding);
    const closure = (outer?.locals ?? [])
      .filter((b) => PRIMS.includes(kindOf(b.type)))
      .map((b) => ({ ...b, mutable: false }));
    const parents = g.refs(outer ?? g.topScope(), (b) => kindOf(b.type) === "cls" && b.type.ctor !== null);
    const parent = parents.length > 0 && g.chance(0.35) ? pick(parents) : null;
    const nullBase = !parent && g.chance(0.07);
    const type = {
      t: "cls",
      binding,
      parentType: parent?.type ?? null,
      fields: { ...(parent?.type.fields ?? {}) }, // public instance fields: name -> "num"
      methods: { ...(parent?.type.methods ?? {}) }, // name -> { kind: method | gen | async | asyncgen, n, writes }
      accessors: { ...(parent?.type.accessors ?? {}) }, // name -> { get: writes | null, set: writes | null }
      statics: { ...(parent?.type.statics ?? {}) },
      staticMethods: { ...(parent?.type.staticMethods ?? {}) },
      staticAccessors: { ...(parent?.type.staticAccessors ?? {}) },
      ctor: nullBase ? null : { n: parent?.type.ctor.n ?? 0, writes: new Set(parent?.type.ctor.writes ?? []) },
      peeks: [...(parent?.type.peeks ?? [])], // pure methods that return the private state, for the observer
      staticPeek: null,
    };
    binding.type = type;
    const self = {
      cls: type,
      me,
      binding,
      parentType: type.parentType,
      privateFields: [],
      privateGetters: {}, // name -> writes
      privateMethods: {},
      staticPrivateFields: [],
      staticPrivateMethods: {},
      staticSide: false,
      beforeSuper: false,
    };
    /** A scope for the body of a member; `fn` is `null` where there is no function (a static block, an initializer). */
    const scopeFor = (patch, ps = [], fn = {}) => ({
      ...g.topScope(),
      locals: [...closure, ...ps],
      top: false,
      inFunction: true,
      fn: fn === null ? null : { ret: "num", async: false, generator: false, arguments: true, ...fn },
      frozen: new Set(outer?.frozen ?? []),
      nest: 2,
      self: { ...self, ...patch },
    });
    const body = (s, tail, n = 1 + g.int(2)) => {
      const parts = [];
      for (let i = 0; i < n; i++) parts.push(g.stmt(s));
      parts.push(tail);
      return `{ ${parts.join(" ")} }`;
    };
    const params = (n) => Array.from({ length: n }, () => numeric(g.fresh("p")));
    const free = () => {
      const names = FIELDS.filter((f) => !(f in type.fields));
      return names.length > 0 ? pick(names) : null;
    };
    const members = { fields: [], ctor: [], methods: [], block: [] };
    const written = []; // what each member that may be called later writes

    // fields: an initializer sees the scope around the class, not `this`
    const initializer = () => {
      const s = scopeFor(null, [], null);
      s.self = null;
      s.nest = 0;
      return { text: N(s, 1), s };
    };
    if (!nullBase) {
      for (let i = 0, n = g.int(4); i < n; i++) {
        const kind = pick(["field", "field", "no-init", "private", "private", "computed", "arrow"]);
        if (kind === "private") {
          const p = g.fresh("p");
          const { text, s } = initializer();
          merge(type.ctor.writes, s.writes);
          members.fields.push(`#${p} = ${text};`);
          self.privateFields.push(p);
        } else if (kind === "arrow") {
          const name = g.fresh("m");
          const ps = params(1);
          const s = scopeFor({}, ps, { arrow: true, arguments: false });
          members.fields.push(`${name} = (${names(ps)}) => ${body(s, `return ${N(s, 2)};`)};`);
          type.methods[name] = { kind: "method", field: true, n: 1, writes: s.writes };
          written.push(s.writes);
        } else {
          const f = free();
          if (f === null) continue;
          type.fields[f] = "num";
          if (kind === "no-init") members.fields.push(`${f};`);
          else {
            const { text, s } = initializer();
            merge(type.ctor.writes, s.writes);
            members.fields.push(kind === "computed" ? `[${JSON.stringify(f)}] = ${text};` : `${f} = ${text};`);
          }
        }
      }
    }
    // static fields run when the class is defined, in order, with the name of the class already bound
    for (let i = 0, n = g.int(3); i < n; i++) {
      const { text, s } = initializer();
      if (outer) merge(outer.writes, s.writes);
      if (g.chance(0.65)) {
        const f = g.fresh("s");
        type.statics[f] = "num";
        members.fields.push(`static ${f} = ${text};`);
      } else {
        const p = g.fresh("p");
        self.staticPrivateFields.push(p);
        members.fields.push(`static #${p} = ${text};`);
      }
    }
    // pure methods that return what the private fields hold: the observers print them
    if (self.privateFields.length > 0) {
      const name = g.fresh("m");
      members.methods.push(
        `${name}() { return [${self.privateFields.map((p) => `this.#${p}`).join(", ")}].join(","); }`
      );
      type.peeks.push(name);
    }
    if (self.staticPrivateFields.length > 0) {
      const name = g.fresh("m");
      members.methods.push(
        `static ${name}() { return [${self.staticPrivateFields.map((p) => `this.#${p}`).join(", ")}].join(","); }`
      );
      type.staticPeek = name;
    }

    // methods and accessors, each of which may call the ones before it
    const KINDS = nullBase
      ? [["static method", 3], ["static private method", 1], ["static getter", 2]]
      : [["method", 5], ["private method", 2], ["static method", 2], ["static private method", 1], ["getter", 2], ["accessor pair", 2], ["static getter", 1], ["private getter", 1], ["generator", 1], ["async", 1], ["async generator", 1]]; // prettier-ignore
    for (let i = 0, n = 1 + g.int(3); i < n; i++) {
      const kind = g.weighted(KINDS.map(([k, w]) => ({ k, w }))).k;
      const name = g.fresh("m");
      const staticSide = kind.startsWith("static");
      const scope = (ps, fn) => scopeFor({ staticSide }, ps, fn);
      const key = (k) => (g.chance(0.2) ? `["m" + ${k.slice(1)}]` : k); // a computed key that spells the same name
      if (kind.endsWith("method")) {
        const ps = params(g.int(3));
        const s = scope(ps);
        const header = kind.includes("private") ? `#${name}` : key(name);
        members.methods.push(`${staticSide ? "static " : ""}${header}(${names(ps)}) ${body(s, `return ${N(s, 2)};`)}`);
        const info = { kind: "method", n: ps.length, writes: s.writes };
        written.push(s.writes);
        if (kind === "method") type.methods[name] = info;
        else if (kind === "static method") type.staticMethods[name] = info;
        else if (kind === "private method") self.privateMethods[name] = info;
        else self.staticPrivateMethods[name] = info;
      } else if (kind.endsWith("getter")) {
        const s = scope([]);
        members.methods.push(
          `${staticSide ? "static " : ""}get ${kind === "private getter" ? `#${name}` : name}() ${body(s, `return ${N(s, 2)};`)}`
        );
        written.push(s.writes);
        if (kind === "getter") type.accessors[name] = { get: s.writes, set: null };
        else if (kind === "static getter") {
          // a setter next to it, sometimes: it takes a number and counts it in the first static field
          const ps = params(1);
          const t = scope(ps);
          const field = Object.keys(type.statics)[0];
          type.staticAccessors[name] = { get: s.writes, set: null };
          if (me && g.chance(0.5)) {
            members.methods.push(
              `static set ${name}(${names(ps)}) ${body(t, field ? `${me}.${field} += ${local(ps[0].name)};` : "")}`
            );
            written.push(t.writes);
            type.staticAccessors[name].set = t.writes;
          }
        } else self.privateGetters[name] = s.writes;
      } else if (kind === "accessor pair") {
        const s = scope([]);
        const getter = `get ${name}() ${body(s, `return ${N(s, 2)};`)}`;
        const ps = params(1);
        const t = scope(ps);
        const field = Object.keys(type.fields)[0];
        members.methods.push(
          getter,
          `set ${name}(${names(ps)}) ${body(t, field ? `this.${field} = ${local(ps[0].name)};` : "")}`
        );
        written.push(s.writes, t.writes);
        type.accessors[name] = { get: s.writes, set: t.writes };
      } else {
        const ps = params(1);
        const generator = kind !== "async";
        const async = kind !== "generator";
        const s = scope(ps, { generator, async });
        if (async) g.late = true;
        members.methods.push(
          `${async ? "async " : ""}${generator ? "*" : ""}${name}(${names(ps)}) ${body(s, `return ${N(s, 2)};`)}`
        );
        written.push(s.writes);
        const info = { kind: !async ? "gen" : generator ? "asyncgen" : "async", n: 1, writes: s.writes };
        type.methods[name] = info;
      }
    }

    // the constructor: `super(…)` first in a derived class, which sometimes has the default one
    if (type.ctor && (!parent || g.chance(0.6))) {
      const ps = params(g.int(3));
      const s = scopeFor({ beforeSuper: !!parent }, ps, { ret: "void" });
      const parts = [];
      if (parent) {
        parts.push(`super(${argList(s, 1, parent.type.ctor.n)});`);
        s.self = { ...self, beforeSuper: false };
      }
      for (let i = 0, n = g.int(3); i < n; i++) parts.push(g.stmt(s));
      const f = free();
      if (f !== null && g.chance(0.4)) {
        parts.push(`this.${f} = ${me && g.chance(0.5) ? `new.target === ${me}` : "new.target !== undefined"} ? 1 : 2;`);
        type.fields[f] = "num";
      }
      type.ctor.n = ps.length;
      merge(type.ctor.writes, s.writes);
      members.ctor.push(`constructor(${names(ps)}) { ${parts.join(" ")} }`);
    }
    // a static block runs when the class is defined, after the static fields before it and with every method in place
    if (g.chance(nullBase ? 0.6 : 0.3)) {
      const s = scopeFor({ staticSide: true }, [], null);
      s.nest = 1;
      const text = [g.stmt(s), g.stmt(s)].join(" ");
      if (outer) merge(outer.writes, s.writes);
      members.block.push(`static { ${text} }`);
    }
    if (outer) for (const writes of written) merge(outer.writes, writes);

    const heritage = nullBase
      ? " extends null"
      : parent
        ? ` extends ${pick([g.ref(parent), g.ref(parent), `(0, ${g.ref(parent)})`, `(true ? ${g.ref(parent)} : ${g.ref(parent)})`])}`
        : "";
    const head = expression ? `class${inner ? ` ${me}` : ""}` : `class ${me}`;
    const text = `${head}${heritage} { ${[...members.fields, ...members.ctor, ...members.methods, ...members.block].join(" ")} }`;
    return { text: expression ? `const ${g.ref(binding)} = ${text};` : text, binding, type };
  }

  // ---- units
  g.makers.push({
    name: "class declaration",
    w: 4,
    make: (g) => {
      const made = makeClass({});
      g.add("class declaration", made.text, [made.binding]);
    },
  });
  g.makers.push({
    name: "class expression",
    w: 2,
    make: (g) => {
      const made = makeClass({ expression: true });
      g.add("class expression", made.text, [made.binding]);
    },
  });
  // an object that holds a class, so that `new h.K(…)` is written too
  g.makers.push({
    name: "class holder",
    w: 2,
    ok: (g) => constructible(g.topScope()).length > 0,
    make: (g) => {
      const c = pick(constructible(g.topScope()));
      const h = g.binding("o", { t: "holder", cls: c });
      g.add("class holder", `const ${g.ref(h)} = { K: ${g.ref(c)} };`, [h]);
    },
  });
  g.makers.push({
    name: "instance",
    w: 4,
    ok: (g) => constructible(g.topScope()).length > 0,
    make: (g) => {
      const s = g.topScope();
      const holders = g.refs(s, (b) => kindOf(b.type) === "holder");
      const holder = holders.length > 0 && g.chance(0.5) ? pick(holders) : null;
      const c = holder ? holder.type.cls : pick(constructible(s));
      const via = holder ? `${g.ref(holder)}.K` : g.ref(c);
      const kind = pick(["const", "const", "let"]);
      const i = g.binding("i", { t: "obj", fields: { ...c.type.fields }, cls: c.type }, { kind });
      const n = c.type.ctor.n;
      const list = argList(s, 1, n);
      const form = g.int(6);
      const text =
        form === 0 && n === 0
          ? `new ${via}`
          : form === 1
            ? `new (${via})(${list})`
            : form === 2 && n > 0
              ? `new ${via}(...[${list}])`
              : `new ${via}(${list})`;
      g.add("instance", `${kind} ${g.ref(i)} = ${text};`, [i]);
    },
  });

  // ---- observers: the statics of a class, and the private state of an instance
  g.observers.cls = (g, b) => {
    const parts = Object.keys(b.type.statics).map((f) => `${g.ref(b)}.${f}`);
    if (b.type.staticPeek) parts.push(`${g.ref(b)}.${b.type.staticPeek}()`);
    return parts.length === 0 ? null : `[${parts.join(", ")}].join(",")`;
  };
  g.observers.holder = () => null;
  const observeObject = g.observers.obj;
  g.observers.obj = (g, b) => {
    const fields = observeObject ? observeObject(g, b) : null;
    const peeks = (b.type.cls?.peeks ?? []).map((m) => `${g.ref(b)}.${m}()`);
    return peeks.length === 0 ? fields : `[${[fields ?? '""', ...peeks].join(", ")}].join("|")`;
  };
}
