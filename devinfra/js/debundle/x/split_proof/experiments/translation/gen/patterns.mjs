// Family `patterns`: destructuring in declarations, assignments, parameters, callbacks and loop heads, with defaults, rest,
// holes, computed keys and nesting; spread in calls and arrays; values that may be `null` and the optional chains over them
// (`?.` on members, indexes and calls, `delete a?.b`); logical assignment on array elements and members; `for … in` and
// `for … of` into existing targets; declarations with several declarators; block-level functions.
//
// A pattern is built together with a value it matches, so that the type of everything it binds is known and the match
// cannot throw: `{ pattern, value(scope), binds, missing }`. A leaf may have a default, and then the value may be missing
// there, which makes the default the value.
import { kindOf, local } from "./core.mjs";

const PRIMS = ["num", "str", "bool"];
const KEYS = ["a", "b", "c", "d", "x", "y"];
const PREFIX = { num: "v", str: "s", bool: "q", obj: "o", arr: "a" };

export function install(g) {
  const pick = (xs) => g.pick(xs);
  const N = (s, d = 2) => g.num(s, d);
  const S = (s, d = 2) => g.str(s, d);
  const B = (s, d = 2) => g.bool(s, d);
  const clamp = (text) => `(${text}).slice(0, 24)`;
  const valueOf = (type, s, d = 1) => (type === "str" ? clamp(S(s, d)) : g.expr(type, s, d));
  const isPrim = (b) => PRIMS.includes(kindOf(b.type));
  const writable = (s) => (b) => b.mutable && !s.frozen.has(b.name);
  const usable = (s, writes) => ![...writes].some((n) => s.frozen.has(n));
  const mayIterate = (s) => !(s.fn?.generator || s.fn?.async);

  // ---- a pattern and a value it matches
  /**
   * `declare(type)` makes the binding of a leaf; `scope` is where the defaults are evaluated; `top` forces an object or
   * array pattern, because a leaf alone is not a pattern.
   */
  function pair(depth, declare, scope, top = true) {
    const form = top
      ? pick(["object", "array"])
      : depth > 0
        ? g.weighted([
            { w: 5, k: "leaf" },
            { w: 3, k: "object" },
            { w: 3, k: "array" },
          ]).k
        : "leaf";
    if (form === "leaf") {
      const type = g.chance(0.8) ? "num" : pick(["str", "bool"]);
      const dflt = g.chance(0.3) ? valueOf(type, scope) : null; // before the binding: a default does not see what it defaults
      const missing = dflt !== null && g.chance(0.4);
      const b = declare(type);
      return {
        pattern: `${g.ref(b)}${dflt !== null ? ` = ${dflt}` : ""}`,
        value: (s, d = 1) => (missing ? "undefined" : valueOf(type, s, d)),
        binds: [b],
        missing,
        leaf: true,
      };
    }
    /** A part of a pattern. One that is not a leaf may have a default of its own: an object or array that matches it. */
    const nested = () => {
      const before = { ...scope, locals: [...scope.locals] };
      const child = pair(depth - 1, declare, scope, false);
      return !child.leaf && g.chance(0.2) ? { ...child, pattern: `${child.pattern} = ${child.value(before)}` } : child;
    };
    if (form === "object") {
      const props = [];
      const used = new Set();
      for (let i = 0, n = 1 + g.int(3); i < n; i++) {
        const name = pick(KEYS.filter((k) => !used.has(k)));
        used.add(name);
        // identifier, string, number and computed keys all spell the same way in a pattern and in the object it matches
        const key = pick([name, name, JSON.stringify(`${name}-k`), String(10 + i), `[${JSON.stringify(name)}]`]);
        const child = nested();
        props.push({ key, child, omit: child.missing && g.chance(0.5) });
      }
      const extras = g.chance(0.25) ? Array.from({ length: 1 + g.int(2) }, (_, i) => `r${i}`) : [];
      const binds = props.flatMap((p) => p.child.binds);
      const rest =
        extras.length > 0 ? declare({ t: "obj", fields: Object.fromEntries(extras.map((e) => [e, "num"])) }) : null;
      if (rest) binds.push(rest);
      return {
        pattern: `{ ${[...props.map((p) => `${p.key}: ${p.child.pattern}`), ...(rest ? [`...${g.ref(rest)}`] : [])].join(", ")} }`,
        value: (s, d = 1) =>
          `{ ${[
            ...props.filter((p) => !p.omit).map((p) => `${p.key}: ${p.child.value(s, d)}`),
            ...extras.map((e) => `${e}: ${N(s, d)}`),
          ].join(", ")} }`,
        binds,
        missing: false,
      };
    }
    const items = [];
    for (let i = 0, n = 1 + g.int(3); i < n; i++) items.push(i < n - 1 && g.chance(0.15) ? null : nested());
    const rest = g.chance(0.25) ? declare({ t: "arr", elem: "num" }) : null;
    const binds = items.flatMap((c) => c?.binds ?? []);
    if (rest) binds.push(rest);
    return {
      pattern: `[${[...items.map((c) => c?.pattern ?? ""), ...(rest ? [`...${g.ref(rest)}`] : [])].join(", ")}]`,
      value: (s, d = 1) =>
        `[${[...items.map((c) => c?.value(s, d) ?? N(s, d)), ...(rest ? [N(s, d), N(s, d)] : [])].join(", ")}]`,
      binds,
      missing: false,
    };
  }
  const keyOf = (f) => (/^[A-Za-z_$][\w$]*$/.test(f) ? f : JSON.stringify(f));

  // ---- declarations: units and statements
  const declareTop = (kind, bindings) => (type) => {
    const b = g.binding(PREFIX[kindOf(type)], type, { kind });
    bindings.push(b);
    return b;
  };
  g.makers.push({
    name: "destructuring declaration",
    w: 5,
    make: (g) => {
      const s = g.topScope();
      const kind = pick(["const", "const", "let", "var"]);
      const bindings = [];
      const p = pair(2, declareTop(kind, bindings), s);
      g.add("destructuring declaration", `${kind} ${p.pattern} = ${p.value(s)};`, bindings);
    },
  });
  // a pattern over a binding that is already there: an object's fields with the rest, an array, a set or a string
  g.makers.push({
    name: "destructuring of a binding",
    w: 4,
    ok: (g) => g.refs(g.topScope(), (b) => ["obj", "arr", "set"].includes(kindOf(b.type)) && !b.type.opaque).length > 0,
    make: (g) => {
      const s = g.topScope();
      const kind = pick(["const", "let"]);
      const bindings = [];
      const declare = declareTop(kind, bindings);
      const source = pick(g.refs(s, (b) => ["obj", "arr", "set"].includes(kindOf(b.type)) && !b.type.opaque));
      const text = bindingPattern(source, declare, s);
      g.add("destructuring of a binding", `${kind} ${text} = ${g.ref(source)};`, bindings);
    },
  });
  /** The pattern that takes `source` (an object, array or set binding) apart, with a default wherever the value may be missing. */
  function bindingPattern(source, declare, scope) {
    const k = kindOf(source.type);
    if (k === "obj") {
      const entries = Object.entries(source.type.fields);
      const chosen = entries.filter(() => g.chance(0.6));
      const parts = chosen.map(([f, type]) => {
        const b = declare(type);
        const dflt = g.chance(0.5) ? ` = ${valueOf(type, scope)}` : "";
        return `${g.chance(0.2) ? `[${JSON.stringify(f)}]` : keyOf(f)}: ${g.ref(b)}${dflt}`;
      });
      const left = entries.filter(([f]) => !chosen.some(([c]) => c === f));
      if (left.length > 0 && (parts.length === 0 || g.chance(0.5)))
        parts.push(`...${g.ref(declare({ t: "obj", fields: Object.fromEntries(left) }))}`);
      return `{ ${parts.join(", ")} }`;
    }
    const elem = source.type.elem;
    const parts = Array.from({ length: 1 + g.int(3) }, () =>
      g.chance(0.12) ? "" : `${g.ref(declare(elem))} = ${valueOf(elem, scope)}`
    );
    if (parts.at(-1) === "") parts.pop();
    if (k === "arr" && g.chance(0.3)) parts.push(`...${g.ref(declare({ t: "arr", elem }))}`);
    return `[${parts.join(", ")}]`;
  }
  const st = g.stmts;
  // several declarators in one declaration, a pattern among them. Not a unit of its own: the fuzzer's splitter and the checker
  // pair a unit of the generator with one of the front end's, which counts a declarator for each.
  st.push({
    w: 3,
    ok: (g, s) => !s.top,
    make: (g, s) => {
      const kind = pick(["const", "let"]);
      const parts = [];
      const made = [];
      for (let i = 0, n = 2 + g.int(2); i < n; i++) {
        if (g.chance(0.3)) {
          const p = pair(1, (type) => ({ name: g.fresh("l"), scope: "local", type, mutable: kind === "let" }), s);
          parts.push(`${p.pattern} = ${p.value(s)}`);
          made.push(...p.binds);
        } else {
          const type = pick(PRIMS);
          const b = { name: g.fresh("l"), scope: "local", type, mutable: kind === "let" };
          parts.push(`${local(b.name)} = ${valueOf(type, s)}`);
          made.push(b);
        }
      }
      s.locals.push(...made); // a default does not see what the declaration declares
      return `${kind} ${parts.join(", ")};`;
    },
  });
  st.push({
    w: 4,
    ok: (g, s) => !s.top,
    make: (g, s) => {
      const kind = pick(["const", "let"]);
      const made = [];
      const p = pair(2, (type) => ({ name: g.fresh("l"), scope: "local", type, mutable: kind === "let", made }), s);
      const text = `${kind} ${p.pattern} = ${p.value(s)};`;
      s.locals.push(...p.binds); // the pattern's defaults did not see what it binds
      return text;
    },
  });

  // ---- assignment into existing targets: bindings, fields and array elements
  const targets = (s) => [
    ...g.refs(s, (b) => isPrim(b) && writable(s)(b)).map((b) => ({ text: g.ref(b), type: kindOf(b.type), owner: b })),
    ...["num", "str"].flatMap((type) => g.fields(s, type).filter(([b]) => !s.frozen.has(b.name) && !b.type.fixed).map(([b, f]) => ({ text: g.access(b, f), type, owner: b }))),
    ...g.arrays(s, "num").filter((b) => !s.frozen.has(b.name)).map((b) => ({ text: `${g.ref(b)}[0]`, type: "num", owner: b })),
  ]; // prettier-ignore
  /** A pattern that assigns to the targets and a value that matches it. */
  function assignPair(s) {
    const chosen = Array.from({ length: 1 + g.int(3) }, () => pick(targets(s)));
    for (const t of chosen) g.touch(s, t.owner);
    const leafOf = (t) => {
      const dflt = g.chance(0.3);
      const missing = dflt && g.chance(0.4);
      return {
        pattern: `${t.text}${dflt ? ` = ${valueOf(t.type, s)}` : ""}`,
        value: (sc) => (missing ? "undefined" : valueOf(t.type, sc)),
      };
    };
    const leaves = chosen.map(leafOf);
    if (g.chance(0.5)) {
      const keys = chosen.map((_, i) =>
        pick([KEYS[i], KEYS[i], `[${JSON.stringify(KEYS[i])}]`, JSON.stringify(`${KEYS[i]}-k`)])
      );
      return {
        statement: (sc) =>
          `({ ${leaves.map((l, i) => `${keys[i]}: ${l.pattern}`).join(", ")} } = { ${leaves.map((l, i) => `${keys[i]}: ${l.value(sc)}`).join(", ")} });`,
      };
    }
    const holes = leaves.map((_, i) => i < leaves.length - 1 && g.chance(0.2));
    return {
      statement: (sc) => {
        const patterns = leaves.flatMap((l, i) => (holes[i] ? ["", l.pattern] : [l.pattern]));
        const values = leaves.flatMap((l, i) => (holes[i] ? [N(sc, 1), l.value(sc)] : [l.value(sc)]));
        return `[${patterns.join(", ")}] = [${values.join(", ")}];`;
      },
    };
  }
  st.push({
    w: 5,
    ok: (g, s) => targets(s).length > 0,
    make: (g, s) => assignPair(s).statement(s),
  });

  // ---- loop heads: patterns, targets that already exist, `var`
  const jump = (s, extra = {}) => ({ loops: s.loops + 1, breakable: s.breakable + 1, ...extra });
  st.push({
    w: 3,
    ok: (g, s) => s.nest > 0 && mayIterate(s),
    make: (g, s) => {
      const p = pair(1, (type) => ({ name: g.fresh("l"), scope: "local", type, mutable: false }), s);
      const rows = Array.from({ length: 1 + g.int(2) }, () => p.value(s)).join(", ");
      const sc = g.child(s, { locals: [...s.locals, ...p.binds], ...jump(s) });
      return `for (${pick(["const", "let"])} ${p.pattern} of [${rows}]) ${g.block(sc)}`;
    },
  });
  // an object whose own properties are all numbers: no method (an object literal with methods, an instance with arrow fields)
  const allNum = (b) =>
    kindOf(b.type) === "obj" &&
    !b.type.opaque &&
    !b.type.methods &&
    !b.type.cls &&
    Object.values(b.type.fields).every((t) => t === "num");
  st.push({
    w: 2,
    ok: (g, s) => s.nest > 0 && mayIterate(s) && g.refs(s, allNum).length > 0,
    make: (g, s) => {
      const o = pick(g.refs(s, allNum));
      const k = { name: g.fresh("l"), scope: "local", type: "str", mutable: false };
      const v = { name: g.fresh("l"), scope: "local", type: "num", mutable: false };
      const sc = g.child(s, { locals: [...s.locals, k, v], ...jump(s) });
      return `for (const [${local(k.name)}, ${local(v.name)}] of Object.entries(${g.ref(o)})) ${g.block(sc)}`;
    },
  });
  // `for (target of values)`, with a binding, a member or a pattern as the target
  st.push({
    w: 3,
    ok: (g, s) => s.nest > 0 && mayIterate(s) && targets(s).length > 0,
    make: (g, s) => {
      const t = pick(
        targets(s)
          .filter((t) => t.type === "num")
          .concat(targets(s).filter((t) => t.type === "num"))
      );
      if (!t) return "";
      g.touch(s, t.owner);
      const rows = `${N(s, 1)}, ${N(s, 1)}`;
      const sc = g.child(s, jump(s));
      return pick([
        `for (${t.text} of [${rows}]) ${g.block(sc)}`,
        `for ([${t.text}] of [[${N(s, 1)}], [${N(s, 1)}]]) ${g.block(sc)}`,
        `for ({ a: ${t.text} } of [{ a: ${N(s, 1)} }]) ${g.block(sc)}`,
      ]);
    },
  });
  const strTargets = (s) => targets(s).filter((t) => t.type === "str");
  const objects = (s) => g.refs(s, (b) => kindOf(b.type) === "obj" && !s.frozen.has(b.name) && !b.type.opaque);
  st.push({
    w: 2,
    ok: (g, s) => s.nest > 0 && mayIterate(s) && strTargets(s).length > 0 && objects(s).length > 0,
    make: (g, s) => {
      const t = pick(strTargets(s));
      const o = pick(objects(s));
      g.touch(s, t.owner);
      const sc = g.child(s, jump(s, { frozen: new Set([...s.frozen, o.name]) }));
      return `for (${t.text} in ${g.ref(o)}) ${g.block(sc)}`;
    },
  });
  // `for (var k in o)` inside a function: the variable belongs to the function and nothing reads it
  st.push({
    w: 2,
    ok: (g, s) => s.nest > 0 && s.inFunction && mayIterate(s) && objects(s).length > 0,
    make: (g, s) => {
      const o = pick(objects(s));
      const sc = g.child(s, jump(s, { frozen: new Set([...s.frozen, o.name]) }));
      return `for (var ${local(g.fresh("l"))} in ${g.ref(o)}) ${g.block(sc)}`;
    },
  });

  // ---- arrays: logical assignment on elements, spread
  const arrays = (s, elem) => g.arrays(s, elem).filter((b) => !s.frozen.has(b.name));
  st.push({
    w: 3,
    ok: (g, s) => arrays(s, "num").length > 0,
    make: (g, s) => {
      const a = pick(arrays(s, "num"));
      g.touch(s, a);
      const place = `${g.ref(a)}[(Math.abs(${N(s, 1)} | 0) % ${g.ref(a)}.length)]`;
      return `${place} ${pick(["??=", "||=", "&&="])} ${N(s, 1)};`;
    },
  });
  st.push({
    w: 2,
    ok: (g, s) => arrays(s, "num").length > 0,
    make: (g, s) => {
      const a = pick(arrays(s, "num"));
      g.touch(s, a);
      const others = g.arrays(s, "num").filter((b) => b !== a && !s.frozen.has(b.name));
      const spread = others.length > 0 && g.chance(0.6) ? g.ref(pick(others)) : `[${N(s, 1)}, ${N(s, 1)}]`;
      return pick([
        `${g.ref(a)}.push(...${spread});`,
        `${g.ref(a)}.unshift(...${spread});`,
        `${g.ref(a)}.splice(1, 0, ...${spread});`,
      ]);
    },
  });
  const num = g.prods.num;
  const anyNums = (s) => g.arrays(s, "num");
  num.push({
    w: 3,
    ok: (g, s) => anyNums(s).length > 0,
    make: (g, s, d) => {
      const a = g.ref(pick(anyNums(s)));
      return pick([
        `Math.max(...${a}, ${N(s, d)})`,
        `Math.min(${N(s, d)}, ...${a})`,
        `[...${a}, ...[${N(s, d)}]].length`,
        `new Set([...${a}, ${N(s, d)}]).size`,
        `Math.hypot(...${a}.slice(0, 2))`,
      ]);
    },
  });
  const spreadable = (s) =>
    g.refs(
      s,
      (b) =>
        kindOf(b.type) === "fn" &&
        b.type.ret === "num" &&
        !b.type.async &&
        !b.type.thisField &&
        !b.type.tag &&
        !b.type.gen &&
        usable(s, b.type.writes)
    );
  num.push({
    w: 2,
    ok: (g, s) => spreadable(s).length > 0,
    make: (g, s, d) => {
      const f = pick(spreadable(s));
      for (const n of f.type.writes) s.writes.add(n);
      const args = Array.from({ length: f.type.n }, () => N(s, d));
      return pick([
        `${g.ref(f)}(...[${args.join(", ")}])`,
        `${g.ref(f)}(${[...args.slice(0, 1), `...[${args.slice(1).join(", ")}]`].join(", ")})`,
      ]);
    },
  });
  g.prods.str.push({
    w: 1,
    ok: () => true,
    make: (g, s, d) => `[...${S(s, d)}].reverse().join("")`,
  });
  // a callback with a pattern for its parameter
  num.push({
    w: 2,
    ok: (g, s) => g.refs(s, allNum).length > 0,
    make: (g, s, d) => {
      const o = pick(g.refs(s, allNum));
      const acc = { name: g.fresh("l"), scope: "local", type: "num", mutable: false };
      const k = { name: g.fresh("l"), scope: "local", type: "str", mutable: false };
      const v = { name: g.fresh("l"), scope: "local", type: "num", mutable: false };
      const sc = g.callback(s, [acc, k, v], { ret: "num" });
      return `Object.entries(${g.ref(o)}).reduce((${local(acc.name)}, [${local(k.name)}, ${local(v.name)}]) => ${N(sc, d)}, ${N(s, d)})`;
    },
  });

  // ---- functions with patterns for parameters
  const patfn = (s) => g.refs(s, (b) => kindOf(b.type) === "patfn" && usable(s, b.type.writes));
  g.makers.push({
    name: "function with pattern parameters",
    w: 3,
    make: (g) => {
      const sc = {
        ...g.topScope(),
        locals: [],
        top: false,
        inFunction: true,
        fn: { ret: "num", async: false, generator: false, arguments: true },
        nest: 2,
      };
      const declare = (type) => {
        const b = { name: g.fresh("p"), scope: "local", type, mutable: true };
        sc.locals.push(b);
        return b;
      };
      const params = Array.from({ length: 1 + g.int(2) }, () => {
        const before = { ...sc, locals: [...sc.locals] };
        const p = pair(2, declare, sc);
        // a default: the parameter is a pattern that the value of the default matches
        return g.chance(0.3) ? { ...p, pattern: `${p.pattern} = ${p.value(before)}`, dflt: true } : p;
      });
      const rest = g.chance(0.25) ? declare({ t: "arr", elem: "num" }) : null;
      const tail = `return ${N(sc, 2)};`;
      const parts = [];
      for (let i = 0, n = 1 + g.int(2); i < n; i++) parts.push(g.stmt(sc));
      const f = g.binding("f", null, { kind: "function", mutable: false });
      f.type = { t: "patfn", params, rest: rest !== null, writes: sc.writes };
      g.add(
        "function with pattern parameters",
        `function ${g.ref(f)}(${[...params.map((p) => p.pattern), ...(rest ? [`...${g.ref(rest)}`] : [])].join(", ")}) { ${parts.join(" ")} ${tail} }`,
        [f]
      );
    },
  });
  const callPatfn = (s, f, d = 1) => {
    for (const n of f.type.writes) s.writes.add(n);
    const args = f.type.params.map((p) => (p.dflt && g.chance(0.4) ? null : p.value(s, d)));
    if (!f.type.rest) while (args.at(-1) === null) args.pop(); // omitted arguments are the ones at the end
    const list = args.map((a) => a ?? "undefined");
    if (f.type.rest) list.push(N(s, d), N(s, d));
    return `${g.ref(f)}(${list.join(", ")})`;
  };
  num.push({ w: 4, ok: (g, s) => patfn(s).length > 0, make: (g, s, d) => callPatfn(s, pick(patfn(s)), d) });
  g.observers.patfn = () => null;

  // ---- values that may be null, and the optional chains over them
  const maybes = (s) => g.refs(s, (b) => kindOf(b.type) === "maybe");
  const shape = (s, t) =>
    `{ a: ${N(s, 1)}, b: ${clamp(S(s, 1))}${t.inner ? `, inner: { x: ${N(s, 1)} }` : ""}${t.method ? `, f: (x) => x * ${1 + g.int(4)}` : ""} }`;
  g.makers.push({
    name: "maybe object",
    w: 3,
    make: (g) => {
      const s = g.topScope();
      const type = { t: "maybe", inner: g.chance(0.7), method: g.chance(0.6) };
      const kind = pick(["const", "let"]);
      const b = g.binding("n", type, { kind });
      g.add("maybe object", `${kind} ${g.ref(b)} = ${B(s, 1)} ? ${shape(s, type)} : ${pick(["null", "undefined"])};`, [
        b,
      ]);
    },
  });
  g.makers.push({
    name: "maybe function",
    w: 1,
    make: (g) => {
      const b = g.binding("n", { t: "maybefn" }, { kind: "const" });
      g.add("maybe function", `const ${g.ref(b)} = ${B(g.topScope(), 1)} ? (x) => x * ${1 + g.int(4)} : undefined;`, [
        b,
      ]);
    },
  });
  const maybeFns = (s) => g.refs(s, (b) => kindOf(b.type) === "maybefn");
  num.push({
    w: 3,
    ok: (g, s) => maybes(s).length > 0,
    make: (g, s, d) => {
      const m = pick(maybes(s));
      const forms = [`(${g.ref(m)}?.a ?? ${N(s, d)})`, `(${g.ref(m)}?.["a"] ?? ${N(s, d)})`];
      if (m.type.inner) forms.push(`(${g.ref(m)}?.inner?.x ?? ${N(s, d)})`, `(${g.ref(m)}?.inner.x ?? ${N(s, d)})`);
      if (m.type.method)
        forms.push(`(${g.ref(m)}?.f?.(${N(s, d)}) ?? ${N(s, d)})`, `(${g.ref(m)}?.f(${N(s, d)}) ?? ${N(s, d)})`);
      return pick(forms);
    },
  });
  num.push({
    w: 1,
    ok: (g, s) => maybeFns(s).length > 0,
    make: (g, s, d) => `(${g.ref(pick(maybeFns(s)))}?.(${N(s, d)}) ?? ${N(s, d)})`,
  });
  g.prods.str.push({
    w: 2,
    ok: (g, s) => maybes(s).length > 0,
    make: (g, s) => `(${g.ref(pick(maybes(s)))}?.b ?? "s")`,
  });
  g.prods.bool.push({
    w: 2,
    ok: (g, s) => maybes(s).length > 0,
    make: (g, s) => {
      const m = g.ref(pick(maybes(s)));
      return pick([`(${m} == null)`, `(${m}?.a === undefined)`, `(${m}?.inner?.x !== undefined)`]);
    },
  });
  st.push({
    w: 4,
    ok: (g, s) => maybes(s).some((b) => !s.frozen.has(b.name)),
    make: (g, s) => {
      const m = pick(maybes(s).filter((b) => !s.frozen.has(b.name)));
      g.touch(s, m);
      const x = g.ref(m);
      const forms = [
        `if (${x} != null) ${x}.a ??= ${N(s, 1)};`,
        `if (${x} != null) { ${x}.a ||= ${N(s, 1)}; ${x}["a"] &&= ${N(s, 1)}; }`,
        `delete ${x}?.a;`,
        `delete ${x}?.["b"];`,
        `${x}?.a;`,
        `if (${x} != null) ${x}.b += "z";`,
      ];
      if (m.type.inner) forms.push(`if (${x}?.inner) ${x}.inner.x += ${N(s, 1)};`);
      if (m.type.inner) forms.push(`delete ${x}?.inner?.x;`);
      if (m.type.method) forms.push(`${x}?.f?.(${N(s, 1)});`);
      if (m.mutable)
        forms.push(
          `${x} ??= ${shape(s, m.type)};`,
          `${x} &&= ${shape(s, m.type)};`,
          `${x} = ${B(s, 1)} ? ${shape(s, m.type)} : null;`
        );
      return pick(forms);
    },
  });
  g.observers.maybe = (g, b) => {
    const x = g.ref(b);
    return `[${x}?.a, ${x}?.b${b.type.inner ? `, ${x}?.inner?.x` : ""}].join(",")`;
  };
  g.observers.maybefn = (g, b) => `String(${g.ref(b)}?.(1))`;

  // ---- a function declared in a block: it sees the primitives around it and what the block declares before it
  st.push({
    w: 2,
    ok: (g, s) => !s.top && s.nest > 0,
    make: (g, s) => {
      const name = g.fresh("l");
      const p = { name: g.fresh("p"), scope: "local", type: "num", mutable: true };
      const closure = s.locals.filter(isPrim).map((b) => ({ ...b, mutable: false }));
      const fs = {
        ...g.topScope(),
        locals: [...closure, p],
        top: false,
        inFunction: true,
        fn: { ret: "num", async: false, generator: false, arguments: true },
        frozen: new Set(s.frozen),
        nest: 1,
      };
      const text = `function ${local(name)}(${local(p.name)}) { ${g.stmt(fs)} return ${N(fs, 2)}; }`;
      for (const n of fs.writes) s.writes.add(n);
      return `{ ${text} const ${local(g.fresh("l"))} = ${local(name)}(${N(s, 1)}); }`; // the block is the scope of both
    },
  });
}
