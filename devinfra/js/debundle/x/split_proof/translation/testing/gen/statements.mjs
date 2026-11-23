// Family `statements`: declarations of every kind, assignments with every operator, updates, writes to objects, arrays, maps
// and sets, and control flow: `if`, the six loops, `switch` with fall-through, `try`/`catch`/`finally`, labels, `break` and
// `continue`. The statements are bodies: a unit made of one of them runs at the top level, and a function body is made of
// them (`functions.mjs`). Loops count to a constant and never write the variable that counts, a loop over a collection
// does not change it, a `throw` is inside a `try` that catches it, and a `break` or `continue` is where one is legal.
import { kindOf, local } from "./core.mjs";

const FIELD_NAMES = ["a", "b", "c", "d", "x", "y", "n", "k1", "k2"];
const PRIMS = ["num", "str", "bool"];

export function install(g) {
  const st = g.stmts;
  const pick = (xs) => g.pick(xs);
  const N = (s, d = 2) => g.num(s, d);
  const S = (s, d = 2) => g.str(s, d);
  const B = (s, d = 2) => g.bool(s, d);
  const E = (type, s, d = 2) => g.expr(type, s, d);
  const clamp = (text) => `(${text}).slice(0, 24)`; // a string stored in state is short, so that no loop can double it
  const isPrim = (b) => PRIMS.includes(kindOf(b.type));
  const writable = (s) => (b) => b.mutable && !s.frozen.has(b.name);
  const bodyOf = (s, patch = {}, n) => g.block(g.child(s, patch), n);
  const newLocal = (s, type, { mutable = false } = {}) => {
    const b = { name: g.fresh("l"), scope: "local", type, mutable };
    s.locals.push(b);
    return b;
  };
  const withLocal = (s, type) => {
    const b = { name: g.fresh("l"), scope: "local", type, mutable: false };
    return [b, g.child(s, { locals: [...s.locals, b] })];
  };
  const label = () => (g.chance(0.25) ? g.fresh("L") : null);
  const frozen = (s, ...names) => new Set([...s.frozen, ...names]);
  const log = (s) => `console.log("${g.fresh("t")}", ${pick([`String(${N(s, 1)})`, S(s, 1), `String(${B(s, 1)})`])});`;
  const objects = (g, s, ok = () => true) =>
    g.refs(s, (b) => kindOf(b.type) === "obj" && !s.frozen.has(b.name) && ok(b));
  const collections = (g, s, kinds) => g.refs(s, (b) => kinds.includes(kindOf(b.type)) && !s.frozen.has(b.name));

  // ---- declarations in a body
  st.push({
    w: 6,
    ok: (g, s) => !s.top,
    make: (g, s) => {
      const type = pick(PRIMS);
      const kind = pick(["const", "const", "let", ...(s.inFunction ? ["var"] : [])]);
      const text = E(type, s);
      const b = newLocal(s, type, { mutable: kind !== "const" });
      return `${kind} ${local(b.name)} = ${type === "str" ? clamp(text) : text};`;
    },
  });

  // ---- assignments and updates
  st.push({
    w: 8,
    ok: (g, s) => g.refs(s, (b) => isPrim(b) && writable(s)(b)).length > 0,
    make: (g, s) => {
      const b = pick(g.refs(s, (b) => isPrim(b) && writable(s)(b)));
      g.touch(s, b);
      const x = g.ref(b);
      const type = kindOf(b.type);
      if (type === "num")
        return pick([
          `${x} = ${N(s)};`, `${x} += ${N(s)};`, `${x} -= ${N(s)};`, `${x} *= ${N(s, 1)};`, `${x} %= ${N(s, 1)};`, `${x} /= ${N(s, 1)};`,
          `${x} **= ${pick(["2", "0.5"])};`, `${x} <<= (${N(s, 1)} & 7);`, `${x} >>= (${N(s, 1)} & 7);`, `${x} >>>= (${N(s, 1)} & 7);`,
          `${x} |= ${N(s, 1)};`, `${x} &= ${N(s, 1)};`, `${x} ^= ${N(s, 1)};`, `${x} &&= ${N(s, 1)};`, `${x} ||= ${N(s, 1)};`, `${x} ??= ${N(s, 1)};`,
        ]); // prettier-ignore
      if (type === "str")
        return pick([
          `${x} = ${clamp(S(s))};`, `${x} += ${pick(['"a"', `String(${N(s, 1)})`])};`, `${x} ||= ${clamp(S(s, 1))};`,
          `${x} ??= ${clamp(S(s, 1))};`, `${x} &&= ${clamp(S(s, 1))};`,
        ]); // prettier-ignore
      return pick([`${x} = ${B(s)};`, `${x} &&= ${B(s, 1)};`, `${x} ||= ${B(s, 1)};`, `${x} ??= ${B(s, 1)};`]);
    },
  });
  st.push({
    w: 4,
    ok: (g, s) => g.refs(s, (b) => kindOf(b.type) === "num" && writable(s)(b)).length > 0,
    make: (g, s) => {
      const b = pick(g.refs(s, (b) => kindOf(b.type) === "num" && writable(s)(b)));
      g.touch(s, b);
      const x = g.ref(b);
      return pick([`${x}++;`, `${x}--;`, `++${x};`, `--${x};`, `void ${x}++;`, `${N(s, 1)} + ${x}++;`]);
    },
  });
  // writes to the fields of objects (a frozen object takes none, a sealed one no new or deleted field)
  st.push({
    w: 6,
    ok: (g, s) => g.fields(s, "num").some(([b]) => !s.frozen.has(b.name) && !b.type.fixed),
    make: (g, s) => {
      const [b, f] = pick(g.fields(s, "num").filter(([b]) => !s.frozen.has(b.name) && !b.type.fixed));
      g.touch(s, b);
      const place = pick([g.access(b, f), g.access(b, f), `${g.ref(b)}[${JSON.stringify(f)}]`]);
      return pick([
        `${place} = ${N(s)};`, `${place} += ${N(s, 1)};`, `${place}++;`, `--${place};`, `${place} ||= ${N(s, 1)};`, `${place} ??= ${N(s, 1)};`,
        `${place} *= ${N(s, 1)};`, `${place} -= ${N(s, 1)};`, ...(b.type.sealed ? [] : [`delete ${place};`]),
      ]); // prettier-ignore
    },
  });
  st.push({
    w: 3,
    ok: (g, s) => g.fields(s, "str").some(([b]) => !s.frozen.has(b.name) && !b.type.fixed),
    make: (g, s) => {
      const [b, f] = pick(g.fields(s, "str").filter(([b]) => !s.frozen.has(b.name) && !b.type.fixed));
      g.touch(s, b);
      const place = g.access(b, f);
      return pick([`${place} = ${clamp(S(s))};`, `${place} += "z";`, `${place} ||= ${clamp(S(s, 1))};`]);
    },
  });
  st.push({
    w: 2,
    ok: (g, s) => objects(g, s, (b) => !b.type.sealed && !b.type.fixed && !b.type.bare).length > 0,
    make: (g, s) => {
      // a new field: it is a number, so a read of it before the write is NaN and never throws
      const b = pick(objects(g, s, (b) => !b.type.sealed && !b.type.fixed && !b.type.bare));
      g.touch(s, b);
      const f = g.fresh("w");
      b.type.fields[f] = "num";
      return `${g.ref(b)}.${f} = ${N(s)};`;
    },
  });
  // arrays: the length never goes below one
  st.push({
    w: 6,
    ok: (g, s) => collections(g, s, ["arr"]).length > 0,
    make: (g, s) => {
      const b = pick(collections(g, s, ["arr"]));
      g.touch(s, b);
      const a = g.ref(b);
      const type = b.type.elem;
      const v = type === "num" ? N(s, 1) : clamp(S(s, 1));
      const idx = `(Math.abs(${N(s, 1)} | 0) % ${a}.length)`;
      const common = [
        `${a}[${idx}] = ${v};`, `${a}.push(${v});`, `${a}.unshift(${v});`, `${a}.length > 1 && ${a}.pop();`, `${a}.length > 1 && ${a}.shift();`,
        `${a}.splice(${pick(["0", "1"])}, 0, ${v});`, `${a}.reverse();`, `${a}.sort();`, `${a}.fill(${v}, 0, 1);`, `${a}.copyWithin(0, 1);`,
        `${a}.length > 2 && ${a}.splice(1, 1);`, `${a}.push(${v}, ${v});`, `[${a}[0], ${a}[${a}.length - 1]] = [${a}[${a}.length - 1], ${a}[0]];`,
      ]; // prettier-ignore
      return pick(
        type === "num"
          ? [
              ...common,
              `${a}.sort((p, q) => p - q);`,
              `${a}[0]++;`,
              `${a}[0] += ${N(s, 1)};`,
              `${a}[${a}.length] = ${v};`,
            ]
          : common
      );
    },
  });
  st.push({
    w: 4,
    ok: (g, s) => collections(g, s, ["map", "set"]).length > 0,
    make: (g, s) => {
      const b = pick(collections(g, s, ["map", "set"]));
      g.touch(s, b);
      const c = g.ref(b);
      const key = b.type.key ?? b.type.elem;
      const k = key === "num" ? N(s, 1) : clamp(S(s, 1));
      if (kindOf(b.type) === "set")
        return pick([`${c}.add(${k});`, `${c}.delete(${k});`, `${c}.size > 3 && ${c}.clear();`, `${c}.has(${k});`]);
      return pick([
        `${c}.set(${k}, ${N(s, 1)});`, `${c}.set(${k}, ${N(s, 1)}).set(${k}, ${N(s, 1)});`, `${c}.delete(${k});`,
        `${c}.size > 3 && ${c}.clear();`, `${c}.get(${k});`,
      ]); // prettier-ignore
    },
  });

  // ---- control flow
  const nests = (g, s) => s.nest > 0;
  // a generator or an async function does not loop over what other code may change between its steps
  const mayIterate = (s) => !(s.fn?.generator || s.fn?.async);
  st.push({
    w: 6,
    ok: nests,
    make: (g, s) => {
      const t = B(s, 1);
      const form = g.int(4);
      if (form === 0) return `if (${t}) ${bodyOf(s)}`;
      if (form === 1) return `if (${t}) ${bodyOf(s)} else ${bodyOf(s)}`;
      if (form === 2) return `if (${t}) ${bodyOf(s)} else if (${B(s, 1)}) ${bodyOf(s)} else ${bodyOf(s)}`;
      return `if (${t}) ${log(s)}`; // a statement without braces
    },
  });
  // `for`, in its forms: a counter local to the loop, counting down, an empty test that the body ends with `break`, a `var`,
  // and a counter that is a module-scope variable, which the body must leave alone
  st.push({
    w: 5,
    ok: nests,
    make: (g, s) => {
      const lab = label();
      const k = pick(["2", "3"]);
      const counters = g.refs(s, (b) => kindOf(b.type) === "num" && writable(s)(b));
      const form = g.int(6) === 5 ? 5 : g.int(5) === 4 && counters.length > 0 ? 4 : g.int(4);
      const prefix = lab ? `${lab}: ` : "";
      const jump = {
        loops: s.loops + 1,
        breakable: s.breakable + 1,
        labels: lab ? [...s.labels, { name: lab, loop: true }] : s.labels,
      };
      // no test and no update: the body ends with `break`, and a `continue` in it would skip that, so it has none
      if (form === 5)
        return `${prefix}for (;;) { ${g.block(g.child(s, { loops: 0, breakable: s.breakable + 1, labels: lab ? [...s.labels, { name: lab, loop: false }] : s.labels }), 1 + g.int(2)).slice(2, -2)} break; }`;
      if (form === 4) {
        const c = pick(counters);
        g.touch(s, c);
        const x = g.ref(c);
        return `${prefix}for (${x} = 0; ${x} < ${k}; ${x}++) ${bodyOf(s, { ...jump, frozen: frozen(s, c.name) })}`;
      }
      const [i, sc] = withLocal(s, "num");
      const x = local(i.name);
      const inner = g.child(sc, jump);
      const keyword = s.inFunction && g.chance(0.3) ? "var" : "let";
      if (form === 1) return `${prefix}for (let ${x} = ${k}; ${x} > 0; ${x}--) ${g.block(inner)}`;
      if (form === 2)
        return `${prefix}for (let ${x} = 0; ; ${x}++) { if (${x} >= ${k}) break; ${g.block(inner, 1 + g.int(2)).slice(2)}`;
      return `${prefix}for (${keyword} ${x} = 0; ${x} < ${k}; ${x}++) ${g.block(inner)}`;
    },
  });
  // `while` and `do … while` with a counter that the loop condition moves, so that `continue` cannot skip it
  st.push({
    w: 3,
    ok: nests,
    make: (g, s) => {
      const lab = label();
      const k = pick(["2", "3"]);
      const [c, sc] = withLocal(s, "num");
      const jump = {
        loops: s.loops + 1,
        breakable: s.breakable + 1,
        labels: lab ? [...s.labels, { name: lab, loop: true }] : s.labels,
      };
      const body = g.block(g.child(sc, jump));
      const x = local(c.name);
      const loop = g.chance(0.5) ? `while (${x}++ < ${k}) ${body}` : `do ${body} while (++${x} < ${k});`;
      return `{ let ${x} = 0; ${lab ? `${lab}: ` : ""}${loop} }`;
    },
  });
  st.push({
    w: 3,
    ok: (g, s) => nests(g, s) && mayIterate(s) && objects(g, s).length > 0,
    make: (g, s) => {
      const o = pick(objects(g, s));
      const [k, sc] = withLocal(s, "str");
      const body = g.block(g.child(sc, { loops: s.loops + 1, breakable: s.breakable + 1, frozen: frozen(s, o.name) }));
      return `for (${pick(["const", "let"])} ${local(k.name)} in ${g.ref(o)}) ${body}`;
    },
  });
  // `for … of` over an array (its values, its indexes), a set, a map (entries, destructured) or a string
  st.push({
    w: 4,
    ok: (g, s) => nests(g, s) && mayIterate(s),
    make: (g, s) => {
      const all = g.refs(s, (b) => ["arr", "set", "map"].includes(kindOf(b.type)));
      const lab = label();
      const prefix = lab ? `${lab}: ` : "";
      const jump = (names) => ({
        loops: s.loops + 1,
        breakable: s.breakable + 1,
        frozen: frozen(s, ...names),
        labels: lab ? [...s.labels, { name: lab, loop: true }] : s.labels,
      });
      if (all.length === 0 || g.chance(0.15)) {
        const [ch, sc] = withLocal(s, "str");
        return `${prefix}for (const ${local(ch.name)} of ${S(s, 1)}) ${g.block(g.child(sc, jump([])))}`;
      }
      const c = pick(all);
      if (kindOf(c.type) === "map") {
        const k = { name: g.fresh("l"), scope: "local", type: c.type.key, mutable: false };
        const v = { name: g.fresh("l"), scope: "local", type: "num", mutable: false };
        const sc = g.child(s, { locals: [...s.locals, k, v], ...jump([c.name]) });
        return `${prefix}for (const [${local(k.name)}, ${local(v.name)}] of ${pick([g.ref(c), `${g.ref(c)}.entries()`])}) ${g.block(sc)}`;
      }
      const source =
        kindOf(c.type) === "arr"
          ? pick([g.ref(c), g.ref(c), `${g.ref(c)}.values()`, `${g.ref(c)}.keys()`])
          : pick([g.ref(c), `${g.ref(c)}.values()`]);
      const x = {
        name: g.fresh("l"),
        scope: "local",
        type: source.endsWith(".keys()") ? "num" : c.type.elem,
        mutable: false,
      };
      const sc = g.child(s, { locals: [...s.locals, x], ...jump([c.name]) });
      return `${prefix}for (${pick(["const", "let"])} ${local(x.name)} of ${source}) ${g.block(sc)}`;
    },
  });
  // `switch`: fall-through, a default in the middle, a lexical declaration in a case
  st.push({
    w: 3,
    ok: nests,
    make: (g, s) => {
      const inner = g.child(s, { breakable: s.breakable + 1, nest: s.nest - 1, top: false });
      const onStr = g.chance(0.3);
      const values = onStr ? ['"a"', '"b"', '"c"'] : ["0", "1", "2"];
      const discriminant = onStr ? `${S(s, 1)}.slice(0, 1)` : `Math.abs(${N(s, 1)} | 0) % 3`;
      const clauses = [];
      for (let i = 0, n = 1 + g.int(3); i < n; i++) {
        const tests = g.chance(0.3) && i + 1 < values.length ? [values[i], values[i + 1]] : [values[i]];
        let body;
        if (g.chance(0.2)) {
          // a declaration that belongs to the switch block
          const [l, sc] = withLocal(inner, "num");
          body = `let ${local(l.name)} = ${N(inner, 1)}; ${g.stmt(sc)}`;
        } else body = g.block(inner);
        clauses.push(`${tests.map((v) => `case ${v}:`).join(" ")} ${body}${g.chance(0.75) ? " break;" : ""}`);
      }
      if (g.chance(0.7)) clauses.splice(g.chance(0.3) ? 0 : clauses.length, 0, `default: ${g.block(inner)}`);
      return `switch (${discriminant}) { ${clauses.join(" ")} }`;
    },
  });
  // `try`: a `throw` only where a `catch` of the same statement is, and no jump out of a `finally`
  const thrown = () => pick(['new Error("e")', 'new RangeError("r")', "7", '"s"']);
  st.push({
    w: 4,
    ok: nests,
    make: (g, s) => {
      const form = g.int(4); // 0 try/finally, 1 catch without a binding, 2 catch with a pattern or a binding, 3 catch and finally
      const value = thrown();
      const finalizer = g.block(g.child(s, { noJump: true, canThrow: false }));
      const body = g.block(g.child(s, { canThrow: form !== 0 || s.canThrow }));
      const guarded = form === 0 ? body : `{ if (${B(s, 1)}) throw ${value}; ${body.slice(2)}`;
      if (form === 0) return `try ${guarded} finally ${finalizer}`;
      const handlerScope = g.child(s, { canThrow: s.canThrow });
      if (form === 1) return `try ${guarded} catch ${g.block(handlerScope)}`;
      const e = local(g.fresh("l"));
      const m = { name: g.fresh("l"), scope: "local", type: "str", mutable: false };
      const sc = g.child(handlerScope, { locals: [...handlerScope.locals, m] });
      // `{ message }` is safe for a thrown number or string too: they have no `message`, and they are not null
      const param = g.chance(0.5) ? `{ message: ${e} }` : e;
      return `try ${guarded} catch (${param}) { const ${local(m.name)} = String(${e}); ${g.stmt(sc)} }${form === 3 ? ` finally ${finalizer}` : ""}`;
    },
  });
  st.push({ w: 2, ok: (g, s) => s.canThrow, make: (g, s) => `if (${B(s, 1)}) throw ${thrown()};` });
  // labels and jumps
  st.push({
    w: 2,
    ok: nests,
    make: (g, s) => {
      const lab = g.fresh("L");
      const inner = g.child(s, { labels: [...s.labels, { name: lab, loop: false }] });
      return `${lab}: { ${g.stmt(inner)} if (${B(s, 1)}) break ${lab}; ${g.stmt(inner)} }`;
    },
  });
  st.push({
    w: 3,
    ok: (g, s) => (s.breakable > 0 || s.labels.length > 0) && !s.noJump,
    make: (g, s) => {
      const loops = s.labels.filter((l) => l.loop);
      const choices = [];
      if (s.breakable > 0) choices.push("break;");
      if (s.loops > 0) choices.push("continue;");
      if (s.labels.length > 0) choices.push(`break ${pick(s.labels).name};`);
      if (loops.length > 0) choices.push(`continue ${pick(loops).name};`);
      return `if (${B(s, 1)}) ${pick(choices)}`;
    },
  });
  st.push({ w: 2, ok: nests, make: (g, s) => bodyOf(s) });
  st.push({ w: 1, make: () => pick([";", "debugger;"]) });
  st.push({ w: 3, make: (g, s) => log(s) });
  st.push({
    w: 2,
    make: (g, s) => pick([`${N(s, 1)};`, `void ${N(s, 1)};`, `(0, ${N(s, 1)});`, `${S(s, 1)};`, `typeof ${N(s, 1)};`]),
  });

  // ---- units: a statement of the above at the top level, and the declarations
  const prim = (type, w) => ({
    name: `declare ${type}`,
    w,
    make: (g) => {
      const kind = pick(["const", "const", "let", "let", "var"]);
      const text = E(type, g.topScope(), 2);
      const b = g.binding(type === "num" ? "v" : type === "str" ? "s" : "q", type, { kind });
      g.add(`declare ${type}`, `${kind} ${g.ref(b)} = ${type === "str" ? clamp(text) : text};`, [b]);
    },
  });
  g.makers.push(prim("num", 6), prim("str", 3), prim("bool", 2));
  g.makers.push({
    name: "statement",
    w: 12,
    make: (g) => {
      const src = g.stmt(g.topScope());
      if (src) g.add("statement", src);
    },
  });

  // objects: identifier, string, number and computed keys, and a spread of another object
  g.makers.push({
    name: "declare object",
    w: 4,
    make: (g) => {
      const scope = g.topScope();
      const fields = {};
      const parts = [];
      const spread = g.refs(scope, (b) => kindOf(b.type) === "obj" && !b.type.bare && !b.type.opaque);
      if (spread.length > 0 && g.chance(0.25)) {
        const other = pick(spread);
        Object.assign(fields, other.type.fields);
        parts.push(`...${g.ref(other)}`);
      }
      for (let i = 0, n = 1 + g.int(3); i < n; i++) {
        const name = pick(FIELD_NAMES);
        const type = pick(["num", "num", "str", "bool"]);
        const value = type === "str" ? clamp(E(type, scope, 1)) : E(type, scope, 1);
        const form = g.int(8);
        // a key that is not an identifier is read with brackets (`access`)
        if (form === 0) {
          parts.push(`${JSON.stringify(name + "-")}: ${value}`);
          fields[name + "-"] = type;
        } else if (form === 1) {
          parts.push(`[${JSON.stringify(name)}]: ${value}`);
          fields[name] = type;
        } else if (form === 2) {
          parts.push(`5: ${value}`);
          fields["5"] = type;
        } else {
          parts.push(`${name}: ${value}`);
          fields[name] = type;
        }
      }
      const b = g.binding("o", { t: "obj", fields });
      g.add("declare object", `const ${g.ref(b)} = { ${parts.join(", ")} };`, [b]);
    },
  });
  // arrays of numbers (rarely of strings), built in each way
  g.makers.push({
    name: "declare array",
    w: 4,
    make: (g) => {
      const scope = g.topScope();
      const elem = g.chance(0.8) ? "num" : "str";
      const e = () => (elem === "num" ? E("num", scope, 1) : clamp(E("str", scope, 1)));
      const others = g.arrays(scope, elem);
      const forms = [
        () => `[${e()}, ${e()}${g.chance(0.5) ? `, ${e()}` : ""}]`,
        () => `Array.of(${e()}, ${e()})`,
        () => `new Array(${pick(["1", "2", "3"])}).fill(${e()})`,
        () => `[${e()}].concat([${e()}])`,
        () =>
          elem === "num"
            ? `Array.from({ length: ${pick(["1", "2", "3"])} }, (_, i) => i + ${E("num", scope, 1)})`
            : `${S(scope, 1)}.split("").concat(["z"])`,
      ];
      if (elem === "num") forms.push(() => `[${e()}, , ${e()}]`);
      if (others.length > 0)
        forms.push(
          () => `[...${g.ref(pick(others))}, ${e()}]`,
          () => `${g.ref(pick(others))}.slice()`,
          () => `${g.ref(pick(others))}.map((x) => x)`
        );
      const text = pick(forms)();
      const b = g.binding("a", { t: "arr", elem });
      g.add("declare array", `const ${g.ref(b)} = ${text};`, [b]);
    },
  });
  g.makers.push({
    name: "declare map",
    w: 2,
    make: (g) => {
      const scope = g.topScope();
      const key = pick(["num", "str"]);
      const k = () => (key === "num" ? E("num", scope, 1) : clamp(E("str", scope, 1)));
      const entries = Array.from({ length: g.int(3) }, () => `[${k()}, ${E("num", scope, 1)}]`);
      const b = g.binding("m", { t: "map", key, val: "num" });
      g.add("declare map", `const ${g.ref(b)} = new Map(${entries.length > 0 ? `[${entries.join(", ")}]` : ""});`, [b]);
    },
  });
  g.makers.push({
    name: "declare set",
    w: 2,
    make: (g) => {
      const scope = g.topScope();
      const elem = pick(["num", "str"]);
      const e = () => (elem === "num" ? E("num", scope, 1) : clamp(E("str", scope, 1)));
      const arrays = g.arrays(scope, elem);
      const init = arrays.length > 0 && g.chance(0.4) ? g.ref(pick(arrays)) : `[${e()}, ${e()}]`;
      const b = g.binding("e", { t: "set", elem });
      g.add("declare set", `const ${g.ref(b)} = new Set(${init});`, [b]);
    },
  });
  // a `var` in a block, and a loop whose `var` counter is a module-scope binding
  g.makers.push({
    name: "block var",
    w: 1,
    make: (g) => {
      const text = N(g.topScope(), 1);
      const b = g.binding("v", "num", { kind: "var" });
      g.add("block var", `{ var ${g.ref(b)} = ${text}; }`, [b]);
    },
  });
  g.makers.push({
    name: "loop var",
    w: 1,
    make: (g) => {
      const b = g.binding("v", "num", { kind: "var" });
      const body = g.block(g.child(g.topScope(), { loops: 1, breakable: 1, frozen: new Set([b.name]) }));
      g.add("loop var", `for (var ${g.ref(b)} = 0; ${g.ref(b)} < 2; ${g.ref(b)}++) ${body}`, [b]);
    },
  });

  // how a binding is observed: the text of a value that changes nothing
  g.observers.obj = (g, b) => {
    const names = Object.keys(b.type.fields);
    return names.length === 0 ? '""' : `[${names.map((f) => g.access(b, f)).join(", ")}].join(",")`;
  };
  g.observers.arr = (g, b) => `${g.ref(b)}.join(",")`;
  g.observers.map = (g, b) => `[...${g.ref(b)}].join(";")`;
  g.observers.set = (g, b) => `[...${g.ref(b)}].join(";")`;
}
