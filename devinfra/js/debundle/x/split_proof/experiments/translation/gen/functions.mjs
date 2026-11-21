// Family `functions`: declarations, expressions and arrows with default, rest and `arguments` parameters; recursion that
// ends; closures that keep state; objects with methods and `this`; generators (`yield`, `yield*`) and what consumes them;
// async functions (`await`, `then`, `finally`, `for await`) whose continuations run after the program; callbacks of array,
// map and set methods that change shared state; `call`, `apply`, `bind` and tagged templates. A function calls only
// functions declared before it. Every function records which module-scope names it may change (`type.writes`), so that a
// call is not made where a loop iterates, or counts with, one of them.
import { kindOf, local } from "./core.mjs";

export function install(g) {
  const pick = (xs) => g.pick(xs);
  const N = (s, d = 2) => g.num(s, d);
  const B = (s, d = 2) => g.bool(s, d);
  const S = (s, d = 2) => g.str(s, d);
  const numeric = (name, mutable = true) => ({ name, scope: "local", type: "num", mutable });
  const params = (n) => Array.from({ length: n }, () => numeric(g.fresh("p")));
  const names = (ps) => ps.map((p) => local(p.name)).join(", ");

  /** A scope for the body of a function: the module-scope bindings, the parameters, and nothing of the caller's loops. */
  const fnScope = (ps, fn, outer = null) => ({
    ...g.topScope(),
    locals: [...(outer?.locals ?? []), ...ps],
    top: false,
    inFunction: true,
    fn,
    writes: outer?.writes ?? new Set(),
    frozen: outer?.frozen ?? new Set(),
    nest: 2,
  });
  /** A function body: `n` statements and a final `return`. Everything it writes is in `scope.writes`. */
  const body = (scope, tail, n = 1 + g.int(3)) => {
    const parts = [];
    for (let i = 0; i < n; i++) parts.push(g.stmt(scope));
    parts.push(tail);
    return `{ ${parts.join(" ")} }`;
  };
  const callable = (s, pred = () => true) =>
    g.refs(s, (b) => kindOf(b.type) === "fn" && pred(b.type) && ![...b.type.writes].some((n) => s.frozen.has(n)));
  const plain = (t) => t.ret === "num" && !t.async && !t.gen && !t.thisField && !t.tag; // a generator returns an iterator, not a number
  const call = (s, d, f) => {
    for (const n of f.type.writes) s.writes.add(n); // what the callee changes, the caller changes
    const count = f.type.n + g.int(3) - 1;
    return `${g.ref(f)}(${Array.from({ length: Math.max(0, count) }, () => N(s, d)).join(", ")})`;
  };

  // ---- expressions: calls, `await`, `arguments`
  const num = g.prods.num;
  num.push({
    w: 6,
    ok: (g, s) => callable(s, plain).length > 0,
    make: (g, s, d) => call(s, d, pick(callable(s, plain))),
  });
  // a method of an object: it may write the object
  num.push({
    w: 3,
    ok: (g, s) =>
      g.refs(
        s,
        (b) =>
          kindOf(b.type) === "obj" &&
          Object.keys(b.type.methods ?? {}).length > 0 &&
          !s.frozen.has(b.name) &&
          !b.type.fixed
      ).length > 0,
    make: (g, s, d) => {
      const o = pick(
        g.refs(
          s,
          (b) =>
            kindOf(b.type) === "obj" &&
            Object.keys(b.type.methods ?? {}).length > 0 &&
            !s.frozen.has(b.name) &&
            !b.type.fixed
        )
      );
      g.touch(s, o);
      const [m, info] = pick(Object.entries(o.type.methods));
      return `${g.ref(o)}.${m}(${Array.from({ length: info.n }, () => N(s, d)).join(", ")})`;
    },
  });
  // `call`, `apply` and `bind`
  num.push({
    w: 3,
    ok: (g, s) => callable(s, plain).length > 0,
    make: (g, s, d) => {
      const f = pick(callable(s, plain));
      for (const n of f.type.writes) s.writes.add(n);
      const args = Array.from({ length: f.type.n }, () => N(s, d));
      return pick([
        `${g.ref(f)}.call(null${args.map((a) => `, ${a}`).join("")})`,
        `${g.ref(f)}.apply(null, [${args.join(", ")}])`,
        `${g.ref(f)}.bind(null${args
          .slice(0, 1)
          .map((a) => `, ${a}`)
          .join("")})(${args.slice(1).join(", ")})`,
        `Reflect.apply(${g.ref(f)}, null, [${args.join(", ")}])`,
      ]);
    },
  });
  const writableFields = (s) => g.fields(s, "num").filter(([b]) => !b.type.fixed && !s.frozen.has(b.name));
  // a function that reads `this`, called with an object that has the field
  num.push({
    w: 2,
    ok: (g, s) =>
      callable(s, (t) => t.thisField).some((f) => writableFields(s).some(([, field]) => field === f.type.thisField)),
    make: (g, s, d) => {
      const f = pick(
        callable(s, (t) => t.thisField).filter((f) => writableFields(s).some(([, field]) => field === f.type.thisField))
      );
      const [o] = pick(writableFields(s).filter(([, field]) => field === f.type.thisField));
      for (const n of f.type.writes) s.writes.add(n);
      g.touch(s, o);
      return `${g.ref(f)}.call(${g.ref(o)}, ${N(s, d)})`;
    },
  });
  // `arguments`, in a function that is not an arrow
  num.push({
    w: 3,
    ok: (g, s) => s.fn?.arguments,
    make: (g, s) =>
      pick([
        "arguments.length",
        "(arguments[0] ?? 0)",
        "Array.prototype.slice.call(arguments).length",
        "[...arguments].length",
      ]),
    leaf: true,
  });
  // a generator, consumed to the end
  const generators = (s) => callable(s, (t) => t.gen);
  num.push({
    w: 3,
    ok: (g, s) => generators(s).length > 0 && !s.fn?.generator,
    make: (g, s, d) => {
      const f = pick(generators(s));
      for (const n of f.type.writes) s.writes.add(n);
      const it = `${g.ref(f)}(${N(s, d)})`;
      return pick([
        `[...${it}].length`,
        `Array.from(${it}).length`,
        `Math.max(...${it}, 0)`,
        `(${it}.next().value ?? 0)`,
        `new Set(${it}).size`,
        `Array.from(${it}, (x) => x * 2).length`,
      ]);
    },
  });
  g.prods.str.push({
    w: 2,
    ok: (g, s) => generators(s).length > 0 && !s.fn?.generator,
    make: (g, s, d) => {
      const f = pick(generators(s));
      for (const n of f.type.writes) s.writes.add(n);
      return `[...${g.ref(f)}(${N(s, d)})].join(",")`;
    },
  });
  // `await`, in an async function: of a number, of a promise, of an async function that was declared before
  num.push({
    w: 4,
    ok: (g, s) => s.fn?.async,
    make: (g, s, d) => {
      const asyncs = callable(s, (t) => t.async);
      // the executor is an arrow function of its own: nothing in it can `await`
      const forms = [
        `(await ${N(s, d)})`,
        `(await Promise.resolve(${N(s, d)}))`,
        `(await new Promise((resolve) => resolve(${N(g.callback(s, []), d)})))`,
      ];
      if (asyncs.length > 0) {
        const f = pick(asyncs);
        for (const n of f.type.writes) s.writes.add(n);
        forms.push(`(await ${call(s, d, f)})`);
      }
      return pick(forms);
    },
  });

  // ---- statements inside function bodies
  const st = g.stmts;
  const returnType = (s) => s.fn?.ret;
  st.push({
    w: 3,
    ok: (g, s) => s.fn && !s.noJump && !s.fn.generator && ["num", "void"].includes(returnType(s)),
    make: (g, s) => `if (${B(s, 1)}) return${returnType(s) === "num" ? ` ${N(s, 1)}` : ""};`,
  });
  st.push({
    w: 3,
    ok: (g, s) => s.fn?.generator && !s.noJump,
    make: (g, s) =>
      pick([
        `yield ${N(s, 1)};`,
        `yield ${N(s, 1)};`,
        `yield* [${N(s, 1)}, ${N(s, 1)}];`,
        `if (${B(s, 1)}) yield ${N(s, 1)};`,
      ]),
  });
  st.push({
    w: 2,
    ok: (g, s) => s.fn?.generator && !s.noJump && generators(s).length > 0,
    make: (g, s) => {
      const f = pick(generators(s));
      for (const n of f.type.writes) s.writes.add(n);
      return `yield* ${g.ref(f)}(${N(s, 1)});`;
    },
  });
  st.push({ w: 3, ok: (g, s) => s.fn?.async && !s.noJump, make: (g, s) => `await ${N(s, 1)};` });
  st.push({
    w: 2,
    ok: (g, s) => s.fn?.async && !s.noJump,
    make: (g, s) => {
      // `for await` over an array of values and over an async generator
      const [x, sc] = (() => {
        const b = numeric(g.fresh("l"), false);
        return [
          b,
          g.child(s, { locals: [...s.locals, b], loops: s.loops + 1, breakable: s.breakable + 1, nest: s.nest - 1 }),
        ];
      })();
      return `for await (const ${local(x.name)} of [${N(s, 1)}, ${N(s, 1)}]) ${g.block(sc)}`;
    },
  });
  // callbacks of array methods that change shared state; the collection stays as it is while they run
  const callbackScope = (s, ps, ret, extra = {}) => g.callback(s, ps, { ret, ...extra });
  const iterating = (s) => !(s.fn?.generator || s.fn?.async);
  st.push({
    w: 4,
    ok: (g, s) =>
      iterating(s) &&
      g.refs(s, (b) => ["arr", "set", "map"].includes(kindOf(b.type)) && !s.frozen.has(b.name)).length > 0,
    make: (g, s) => {
      const c = pick(g.refs(s, (b) => ["arr", "set", "map"].includes(kindOf(b.type)) && !s.frozen.has(b.name)));
      const kind = kindOf(c.type);
      const x = g.fresh("l");
      const y = g.fresh("l");
      const elem = kind === "map" ? "num" : c.type.elem;
      const ps =
        kind === "arr"
          ? [{ name: x, scope: "local", type: elem, mutable: false }, numeric(y, false)]
          : [
              { name: x, scope: "local", type: elem, mutable: false },
              { name: y, scope: "local", type: kind === "map" ? c.type.key : elem, mutable: false },
            ];
      const sc = callbackScope(s, ps, "void", { frozen: new Set([...s.frozen, c.name]) });
      const bodyText = g.block(sc, 1 + g.int(2));
      return `${g.ref(c)}.forEach((${local(x)}, ${local(y)}) => ${bodyText});`;
    },
  });
  st.push({
    w: 3,
    ok: (g, s) =>
      iterating(s) &&
      g.refs(s, (b) => kindOf(b.type) === "arr" && b.type.elem === "num" && !s.frozen.has(b.name)).length > 0,
    make: (g, s) => {
      const c = pick(g.refs(s, (b) => kindOf(b.type) === "arr" && b.type.elem === "num" && !s.frozen.has(b.name)));
      const x = numeric(g.fresh("l"), false);
      const y = numeric(g.fresh("l"), false);
      const frozen = new Set([...s.frozen, c.name]);
      const arrow = (ret, ps, tail) => {
        const sc = callbackScope(s, ps, ret, { frozen });
        const stmts = g.stmt(sc);
        return `(${ps.map((p) => local(p.name)).join(", ")}) => { ${stmts} return ${tail(sc)}; }`;
      };
      const a = g.ref(c);
      return pick([
        `${a}.map(${arrow("num", [x], (sc) => N(sc, 1))});`,
        `${a}.filter(${arrow("bool", [x], (sc) => B(sc, 1))});`,
        `${a}.some(${arrow("bool", [x], (sc) => B(sc, 1))});`,
        `${a}.every(${arrow("bool", [x, y], (sc) => B(sc, 1))});`,
        `${a}.find(${arrow("bool", [x], (sc) => B(sc, 1))});`,
        `${a}.findLast(${arrow("bool", [x], (sc) => B(sc, 1))});`,
        `${a}.flatMap(${arrow("num", [x], (sc) => `[${N(sc, 1)}]`)});`,
        `${a}.reduce(${arrow("num", [x, y], (sc) => N(sc, 1))}, 0);`,
        `[...${a}].sort(${arrow("num", [x, y], (sc) => N(sc, 1))});`,
        `Array.from(${a}, ${arrow("num", [x, y], (sc) => N(sc, 1))});`,
      ]);
    },
  });
  // an object's keys and entries
  st.push({
    w: 2,
    ok: (g, s) =>
      iterating(s) && g.refs(s, (b) => kindOf(b.type) === "obj" && !s.frozen.has(b.name) && !b.type.opaque).length > 0,
    make: (g, s) => {
      const o = pick(g.refs(s, (b) => kindOf(b.type) === "obj" && !s.frozen.has(b.name) && !b.type.opaque));
      const k = { name: g.fresh("l"), scope: "local", type: "str", mutable: false };
      const sc = callbackScope(s, [k], "void", { frozen: new Set([...s.frozen, o.name]) });
      return `Object.keys(${g.ref(o)}).forEach((${local(k.name)}) => ${g.block(sc, 1)});`;
    },
  });
  // an immediately invoked function, with and without arguments, and an async one
  st.push({
    w: 3,
    ok: (g, s) => s.nest > 0 && iterating(s),
    make: (g, s) => {
      const x = numeric(g.fresh("l"), true);
      const sc = fnScope([x], { ret: "void", async: false, generator: false, arrow: true }, s);
      const text = body(sc, "return;", 1 + g.int(2));
      return pick([
        `(function () ${body(fnScope([], { ret: "void", async: false, generator: false, arguments: true }, s), "return;")})();`,
        `((${local(x.name)}) => ${text})(${N(s, 1)});`,
        `(() => ${body(fnScope([], { ret: "void", async: false, generator: false, arrow: true }, s), "return;")})();`,
      ]);
    },
  });
  st.push({
    w: 2,
    ok: (g, s) => s.nest > 0 && iterating(s),
    make: (g, s) => {
      g.late = true;
      const sc = fnScope([], { ret: "void", async: true, generator: false, arrow: true }, s);
      return `(async () => ${body(sc, "return;", 2)})();`;
    },
  });

  // ---- units: functions of every kind
  const declare = (tmpl, make) => g.makers.push({ name: tmpl, w: make.w ?? 3, ok: make.ok, make: (g) => make.run(g) });
  declare("function", {
    w: 7,
    run: (g) => {
      const ps = params(g.int(4));
      const scope = fnScope(ps, { ret: "num", async: false, generator: false, arguments: true });
      // defaults, which see the parameters before them and the module scope, and a rest parameter
      const heads = ps.map((p, i) =>
        g.chance(0.3) ? `${local(p.name)} = ${N({ ...scope, locals: ps.slice(0, i) }, 1)}` : local(p.name)
      );
      if (g.chance(0.2)) {
        const rest = { name: g.fresh("p"), scope: "local", type: { t: "arr", elem: "num" }, mutable: true };
        scope.locals.push(rest);
        heads.push(`...${local(rest.name)}`);
      }
      const text = body(scope, `return ${N(scope, 2)};`);
      const f = g.binding("f", null, { kind: "function", mutable: false });
      f.type = { t: "fn", n: ps.length, ret: "num", writes: scope.writes };
      g.add("function", `function ${g.ref(f)}(${heads.join(", ")}) ${text}`, [f]);
    },
  });
  declare("arrow", {
    w: 5,
    run: (g) => {
      const ps = params(g.int(3));
      const scope = fnScope(ps, { ret: "num", async: false, generator: false, arrow: true });
      const f = g.binding("f", null);
      const expression = g.chance(0.5);
      const text = expression ? N(scope, 2) : body(scope, `return ${N(scope, 2)};`);
      f.type = { t: "fn", n: ps.length, ret: "num", writes: scope.writes };
      g.add(
        "arrow",
        `const ${g.ref(f)} = (${names(ps)}) => ${expression && text.startsWith("{") ? `(${text})` : text};`,
        [f]
      );
    },
  });
  declare("function expression", {
    w: 3,
    run: (g) => {
      const ps = params(1 + g.int(2));
      const scope = fnScope(ps, { ret: "num", async: false, generator: false, arguments: true });
      const f = g.binding("f", null);
      const named = g.chance(0.5);
      const self = g.fresh("l");
      // a named expression can call itself: the depth is cut to 3 first
      const guarded = named && g.chance(0.5);
      let text;
      if (guarded) ps[0].mutable = false;
      if (guarded) {
        const depth = ps[0];
        text = `{ ${local(depth.name)} = Math.min(${local(depth.name)}, 3); return ${local(depth.name)} > 0 ? ${local(self)}(${local(depth.name)} - 1${ps
          .slice(1)
          .map((p) => `, ${local(p.name)}`)
          .join("")}) + ${N(scope, 1)} : ${N(scope, 1)}; }`;
      } else text = body(scope, `return ${N(scope, 2)};`);
      f.type = { t: "fn", n: ps.length, ret: "num", writes: scope.writes };
      g.add("function expression", `const ${g.ref(f)} = function ${named ? local(self) : ""}(${names(ps)}) ${text};`, [
        f,
      ]);
    },
  });
  declare("recursion", {
    w: 3,
    run: (g) => {
      const f = g.binding("f", null, { kind: "function", mutable: false });
      const n = numeric(g.fresh("p"), false); // the depth: no statement of the body may write it
      const m = numeric(g.fresh("p"));
      const scope = fnScope([n, m], { ret: "num", async: false, generator: false, arguments: true });
      const me = g.ref(f);
      const x = local(n.name);
      // the depth is cut before the call, so that a caller cannot start a deep recursion
      const step = pick([
        `${me}(${x} - 1, ${local(m.name)} + 1) + ${N(scope, 1)}`,
        `${x} * ${me}(${x} - 1, ${local(m.name)})`,
        `${me}(${x} - 2, ${N(scope, 1)}) + ${me}(${x} - 1, ${local(m.name)})`,
      ]);
      const base = N(scope, 1);
      f.type = { t: "fn", n: 2, ret: "num", writes: scope.writes };
      // a statement of the body may assign the depth, so it is cut after the statements
      g.add(
        "recursion",
        `function ${me}(${x}, ${local(m.name)}) { ${g.stmt(scope)} ${x} = Math.min(${x}, 5); if (!(${x} > 1)) return ${base}; return ${step}; }`,
        [f]
      );
    },
  });
  // a function that reads `this`: called through `call`, never plainly
  declare("this function", {
    w: 2,
    ok: (g) => g.fields(g.topScope(), "num").length > 0,
    run: (g) => {
      const [, field] = pick(g.fields(g.topScope(), "num"));
      const p = numeric(g.fresh("p"));
      const scope = fnScope([p], { ret: "num", async: false, generator: false, arguments: true });
      const f = g.binding("f", null, { kind: "function", mutable: false });
      f.type = { t: "fn", n: 1, ret: "num", writes: scope.writes, thisField: field };
      const read =
        /^[A-Za-z_$][\w$]*$/.test(field) && g.chance(0.5) ? `this.${field}` : `this[${JSON.stringify(field)}]`;
      g.add(
        "this function",
        `function ${g.ref(f)}(${local(p.name)}) { ${read} = ${read} + ${N(scope, 1)}; return ${read} + ${local(p.name)}; }`,
        [f]
      );
    },
  });
  // closures that keep state: a counter, an adder made by a factory, an object whose methods share a variable
  declare("closure counter", {
    w: 3,
    run: (g) => {
      const f = g.binding("f", null);
      f.type = { t: "fn", n: 0, ret: "num", writes: new Set() };
      const start = N(g.topScope(), 1);
      g.add(
        "closure counter",
        `const ${g.ref(f)} = (() => { let n = ${start}; return () => ${pick(["++n", "n++", "(n += 2)"])}; })();`,
        [f]
      );
    },
  });
  declare("closure factory", {
    w: 2,
    run: (g) => {
      const make = g.binding("f", null, { kind: "function", mutable: false });
      const k = numeric(g.fresh("p"), false);
      const made = g.binding("f", null);
      made.type = { t: "fn", n: 1, ret: "num", writes: new Set() };
      make.type = { t: "factory", writes: new Set() };
      const x = g.fresh("l");
      const arg = N(g.topScope(), 1);
      g.add(
        "closure factory",
        `function ${g.ref(make)}(${local(k.name)}) { let total = ${local(k.name)}; return (${local(x)}) => (total += ${local(x)}); }`,
        [make]
      );
      g.add("closure factory call", `const ${g.ref(made)} = ${g.ref(make)}(${arg});`, [made]);
    },
  });
  declare("object with methods", {
    w: 3,
    run: (g) => {
      const scope = g.topScope();
      const field = pick(["a", "b", "c"]);
      const methods = {};
      const parts = [`${field}: ${N(scope, 1)}`];
      const p = g.fresh("p");
      const sc = fnScope([numeric(p)], { ret: "num", async: false, generator: false, arguments: true });
      const forms = [
        [`bump`, `bump(${local(p)}) { this.${field} += ${local(p)}; return this.${field}; }`, 1],
        [`peek`, `peek() { return this.${field} * 2; }`, 0],
        [`twice`, `twice: (${local(p)}) => ${local(p)} * 2`, 1],
      ];
      for (const [name, text, n] of forms) {
        if (!g.chance(0.7)) continue;
        parts.push(text);
        methods[name] = { n };
      }
      if (Object.keys(methods).length === 0) {
        parts.push(`peek() { return this.${field} * 2; }`);
        methods.peek = { n: 0 };
      }
      void sc;
      const o = g.binding("o", { t: "obj", fields: { [field]: "num" }, methods });
      g.add("object with methods", `const ${g.ref(o)} = { ${parts.join(", ")} };`, [o]);
    },
  });
  // generators
  declare("generator", {
    w: 4,
    run: (g) => {
      const p = numeric(g.fresh("p"));
      const scope = fnScope([p], { ret: "num", async: false, generator: true });
      const f = g.binding("f", null, { kind: "function", mutable: false });
      const i = g.fresh("l");
      const loop = g.chance(0.6)
        ? (() => {
            const inner = g.child(scope, {
              locals: [...scope.locals, numeric(i, false)],
              loops: 1,
              breakable: 1,
              nest: 1,
            });
            return `for (let ${local(i)} = 0; ${local(i)} < ${pick(["2", "3"])}; ${local(i)}++) { ${g.stmt(inner)} yield ${local(i)} + ${N(inner, 1)}; }`;
          })()
        : "";
      const text = body(scope, `return ${N(scope, 1)};`, g.int(3));
      f.type = { t: "fn", n: 1, ret: "num", writes: scope.writes, gen: true };
      g.add("generator", `function* ${g.ref(f)}(${local(p.name)}) { ${loop} ${text.slice(2)}`, [f]);
    },
  });
  declare("generator object", {
    w: 2,
    ok: (g) => callable(g.topScope(), (t) => t.gen).length > 0,
    run: (g) => {
      const s = g.topScope();
      const f = pick(callable(s, (t) => t.gen));
      const it = g.binding("it", { t: "iter", writes: f.type.writes });
      g.add("generator object", `const ${g.ref(it)} = ${g.ref(f)}(${N(s, 1)});`, [it]);
    },
  });
  g.makers.push({
    name: "advance iterator",
    w: 3,
    ok: (g) => g.top.some((b) => kindOf(b.type) === "iter"),
    make: (g) => {
      const it = pick(g.top.filter((b) => kindOf(b.type) === "iter"));
      g.add(
        "advance iterator",
        pick([
          `${g.ref(it)}.next();`,
          `console.log("n", ${g.ref(it)}.next().value);`,
          `${g.ref(it)}.next(${N(g.topScope(), 1)});`,
          `${g.ref(it)}.return(${N(g.topScope(), 1)});`,
        ])
      );
    },
  });
  // async functions, and the code that starts them
  declare("async function", {
    w: 3,
    run: (g) => {
      g.late = true;
      const p = numeric(g.fresh("p"));
      const scope = fnScope([p], { ret: "num", async: true, generator: false, arguments: true });
      const f = g.binding("f", null, { kind: "function", mutable: false });
      const text = body(scope, `return ${N(scope, 2)};`);
      f.type = { t: "fn", n: 1, ret: "num", writes: scope.writes, async: true };
      g.add("async function", `async function ${g.ref(f)}(${local(p.name)}) ${text}`, [f]);
    },
  });
  declare("async arrow", {
    w: 2,
    run: (g) => {
      g.late = true;
      const scope = fnScope([], { ret: "num", async: true, generator: false, arrow: true });
      const f = g.binding("f", null);
      f.type = { t: "fn", n: 0, ret: "num", writes: scope.writes, async: true };
      g.add("async arrow", `const ${g.ref(f)} = async () => ${body(scope, `return ${N(scope, 2)};`)};`, [f]);
    },
  });
  g.makers.push({
    name: "start async",
    w: 4,
    ok: (g) => callable(g.topScope(), (t) => t.async).length > 0,
    make: (g) => {
      g.late = true;
      const s = g.topScope();
      const f = pick(callable(s, (t) => t.async));
      const v = numeric(g.fresh("l"), false);
      const sc = callbackScope(s, [v], "void");
      const start = call(s, 1, f);
      g.add(
        "start async",
        pick([
          `${start};`,
          `void ${start};`,
          `${start}.then((${local(v.name)}) => ${g.block(sc, 1)});`,
          `${start}.finally(() => ${g.block(callbackScope(s, [], "void"), 1)});`,
          `Promise.all([${start}, ${call(s, 1, f)}]).then((${local(v.name)}) => ${g.block(sc, 1)});`,
        ])
      );
    },
  });
  // tagged templates
  declare("tag function", {
    w: 1,
    run: (g) => {
      const f = g.binding("f", null, { kind: "function", mutable: false });
      f.type = { t: "fn", n: 0, ret: "num", writes: new Set(), tag: true };
      g.add(
        "tag function",
        `function ${g.ref(f)}(strings, ...values) { return strings.length + values.length * ${pick(["2", "3"])}; }`,
        [f]
      );
    },
  });
  num.push({
    w: 2,
    ok: (g, s) => callable(s, (t) => t.tag).length > 0,
    make: (g, s, d) => `${g.ref(pick(callable(s, (t) => t.tag)))}${"`"}a\${${N(s, d)}}b\${${N(s, d)}}${"`"}`,
  });
  num.push({ w: 1, make: (g, s, d) => `String.raw${"`"}x\\n\${${N(s, d)}}${"`"}.length`, leaf: false });

  // how the iterator and the made function are observed: they are not (their state is not visible), and the objects with methods are by their fields
  g.observers.fn = () => null;
  g.observers.iter = () => null;
  g.observers.factory = () => null;
}
