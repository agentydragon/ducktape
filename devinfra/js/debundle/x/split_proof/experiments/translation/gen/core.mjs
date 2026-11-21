// A generator of programs that can reach any part of the language, for the footprint oracle and the translation fuzzer.
//
// A program is a list of top-level units (statements) that share state, because that is what a split moves. Generation is
// type-directed so that every program completes, is deterministic and leaves its state observable:
//
//   - a binding has a type (`num`, `str`, `bool`, or an object type with the fields it has); an expression is built for a
//     type, from the bindings that are initialized where it runs, so it does not throw;
//   - loops count to a constant, a function calls only functions declared before it, nothing reads the clock or a random
//     number, and nothing patches a standard object (premises P1 and P4);
//   - observers print what a binding holds between the units, and one prints everything at the end.
//
// A family (`expressions.mjs`, `statements.mjs`, …) installs productions for expressions and statements and makers of
// units into a `Gen`. `generateProgram(seed, { families })` builds one program, in the shape the fuzzer's splitter takes:
// `{ units: [{ tmpl, src, defs, uses }], exports, imports }`, where `src` marks module-scope names (`top`) and the unit's own
// names (`local`) so that the splitter can rename and import them. `imports` are lines the splitter repeats in every module: the
// names they bind are never marked, because a module imports them itself.
export const top = (n) => `\u0001${n}\u0002`;
export const local = (n) => `\u0003${n}\u0004`;
const TOP = /\u0001([^\u0002]+)\u0002/g;

export function rng(seed) {
  let a = seed >>> 0;
  const float = () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), a | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  return { float, int: (n) => Math.floor(float() * n) };
}

/** The kind of a type: `num`, `str`, `bool`, or the `t` of a structured type. */
export const kindOf = (type) => (typeof type === "string" ? type : type.t);

export class Gen {
  constructor(seed) {
    this.random = rng(seed);
    this.units = [];
    this.top = []; // module-scope bindings declared so far, in order
    this.counter = 0;
    this.prods = { num: [], str: [], bool: [] };
    this.stmts = [];
    this.makers = [];
    this.observers = {}; // kind -> (g, binding) => expression text, or null
    this.exports = [];
    this.imports = []; // import declarations: the program's first lines, and the first lines of every module it is split into
    this.notes = new Set(); // what the program contains that a family wants to count
    this.late = false; // something runs after the program (a continuation): one more observer waits for it
  }

  // ---- randomness
  int(n) {
    return this.random.int(n);
  }
  chance(p) {
    return this.random.float() < p;
  }
  pick(xs) {
    return xs[this.int(xs.length)];
  }
  /** One of `items` (each with a weight `w`), or `null` if there is none. */
  weighted(items) {
    const total = items.reduce((n, i) => n + i.w, 0);
    if (items.length === 0 || total === 0) return null;
    let x = this.random.float() * total;
    for (const item of items) if ((x -= item.w) < 0) return item;
    return items.at(-1);
  }
  fresh(prefix) {
    return `${prefix}${this.counter++}`;
  }

  // ---- bindings and scopes
  /** The text of a reference to (or the declaration of) a binding: module-scope names are marked for the splitter. */
  ref(b) {
    return b.scope === "top" ? top(b.name) : local(b.name);
  }
  /** Bindings usable where `scope` is: the module-scope ones declared so far and the locals. `want` is a type name or a predicate. */
  refs(scope, want) {
    const ok = typeof want === "function" ? want : (b) => kindOf(b.type) === want;
    return [...this.top, ...scope.locals].filter((b) => !scope.hidden?.has(b.name) && ok(b));
  }
  /** A scope for a nested body: its declarations do not leak out. */
  child(scope, patch = {}) {
    return { ...scope, locals: [...scope.locals], ...patch };
  }
  topScope() {
    return {
      locals: [],
      top: true, // a statement here is a unit: its declarations are module-scope
      loops: 0, // enclosing loops (for `continue`) and loops or switches (for `break`)
      breakable: 0,
      labels: [], // `{ name, loop }`
      inFunction: false,
      fn: null, // the function being generated: `{ ret, async, generator }`
      frozen: new Set(), // names a body must not change (what a loop iterates, what counts)
      writes: new Set(), // module-scope names the body (and what it calls) may change
      canThrow: false, // a `throw` here is caught
      noJump: false, // in a `finally`: no `break`, `continue` or `return`
      nest: 3, // how many more levels of statements may nest
    };
  }
  /** The statement changes the binding: a function that does must not be called where the binding must stay put. */
  touch(scope, b) {
    if (b.scope === "top") scope.writes.add(b.name);
  }

  /**
   * The scope of the body of an arrow function written inside `scope`: it sees its own `locals`, the locals around it and
   * the module scope, and `arguments` of the function around it; none of the loops, `await` or `yield` around it.
   */
  callback(scope, locals, { ret = "void", ...patch } = {}) {
    return this.child(scope, {
      locals: [...scope.locals, ...locals],
      fn: { ret, async: false, generator: false, arrow: true, arguments: scope.fn?.arguments },
      loops: 0,
      breakable: 0,
      labels: [],
      canThrow: false,
      noJump: false,
      top: false,
      inFunction: true,
      nest: Math.min(scope.nest, 1),
      ...patch,
    });
  }

  // ---- the type model: objects have fields, arrays and sets elements, maps keys and values
  /** `[binding, field]` for the objects that have a field of type `ftype` (`fields` is `{ name: type }`). */
  fields(scope, ftype) {
    return this.refs(scope, (b) => kindOf(b.type) === "obj").flatMap((b) =>
      Object.entries(b.type.fields)
        .filter(([, t]) => t === ftype)
        .map(([f]) => [b, f])
    );
  }
  /** The text of a property access of the field: a name that is an identifier is read with a dot. */
  access(b, f) {
    return /^[A-Za-z_$][\w$]*$/.test(f) ? `${this.ref(b)}.${f}` : `${this.ref(b)}[${JSON.stringify(f)}]`;
  }
  arrays(scope, elem) {
    return this.refs(scope, (b) => kindOf(b.type) === "arr" && b.type.elem === elem);
  }
  maps(scope, key) {
    return this.refs(scope, (b) => kindOf(b.type) === "map" && b.type.key === key);
  }
  sets(scope, elem) {
    return this.refs(scope, (b) => kindOf(b.type) === "set" && b.type.elem === elem);
  }

  // ---- expressions
  /** An expression of `type` (`num`, `str` or `bool`) that does not throw, `depth` levels of operators deep at most. */
  expr(type, scope, depth = 2) {
    const candidates = this.prods[type].filter((p) => (depth > 0 || p.leaf) && (!p.ok || p.ok(this, scope)));
    return this.weighted(candidates).make(this, scope, depth - 1);
  }
  num(scope, depth) {
    return this.expr("num", scope, depth);
  }
  str(scope, depth) {
    return this.expr("str", scope, depth);
  }
  bool(scope, depth) {
    return this.expr("bool", scope, depth);
  }

  // ---- statements
  /** One statement for a body, or `""` when no production applies. A declaration adds to `scope.locals`. */
  stmt(scope) {
    const candidates = this.stmts.filter((p) => !p.ok || p.ok(this, scope));
    const production = this.weighted(candidates);
    return production ? production.make(this, scope) : "";
  }
  /** `{ s1 s2 … }`, `n` statements in a scope of its own. */
  block(scope, n = 1 + this.int(3)) {
    const inner = this.child(scope, { nest: scope.nest - 1, top: false });
    const body = [];
    for (let i = 0; i < n; i++) body.push(this.stmt(inner));
    return `{ ${body.join(" ")} }`;
  }

  // ---- units
  /** Add a unit. `defs` are the module-scope names it declares, `bindings` the new module-scope bindings. */
  add(tmpl, src, bindings = []) {
    this.units.push({ tmpl, src, defs: bindings.map((b) => b.name), bindings });
    this.top.push(...bindings);
  }
  /** A new module-scope binding. */
  binding(prefix, type, { kind = "const", mutable = kind !== "const" } = {}) {
    return { name: this.fresh(prefix), scope: "top", type, kind, mutable };
  }

  // ---- observers
  /** An expression for what the binding holds, or `null`: it must not change anything and must not name a function or class. */
  observe(b) {
    const k = kindOf(b.type);
    if (k === "num" || k === "str" || k === "bool") return `String(${this.ref(b)})`;
    return this.observers[k]?.(this, b) ?? null;
  }

  /** Observers between the units, and one of everything at the end; then the shape the splitter takes. */
  finish() {
    const observable = this.top.map((b) => [b, this.observe(b)]).filter(([, text]) => text !== null);
    const declared = (b) => this.units.findIndex((u) => u.defs.includes(b.name));
    // an observer of a binding goes anywhere after the unit that declares it
    const inserts = [];
    for (const [b, text] of observable)
      if (this.chance(0.55)) inserts.push({ at: declared(b) + 1 + this.int(this.units.length - declared(b)), b, text });
    inserts.sort((a, b) => b.at - a.at);
    inserts.forEach(({ at, b, text }, i) =>
      this.units.splice(at, 0, { tmpl: "observe", src: `console.log("o${i}", ${text});`, defs: [], bindings: [] })
    );
    if (observable.length > 0)
      this.units.push({
        tmpl: "final",
        src: `console.log("final", [${observable.map(([, text]) => text).join(", ")}].join("|"));`,
        defs: [],
        bindings: [],
      });
    if (this.late && observable.length > 0)
      this.units.push({
        tmpl: "late",
        src: `setTimeout(() => console.log("late", [${observable.map(([, text]) => text).join(", ")}].join("|")), 0);`,
        defs: [],
        bindings: [],
      });
    const exportable = this.top.filter((b) => b.kind !== "var");
    const wanted = this.chance(0.3) ? 1 + this.int(2) : 0;
    this.exports = [];
    for (let i = 0; i < wanted && exportable.length > 0; i++) this.exports.push(this.pick(exportable).name);
    this.exports = [...new Set(this.exports)];
    for (const u of this.units)
      u.uses = [...new Set([...u.src.matchAll(TOP)].map((m) => m[1]))].filter((n) => !u.defs.includes(n));
    return { units: this.units, exports: this.exports, imports: this.imports, notes: [...this.notes] };
  }
}

/** Build one program with the families `installers` (functions that take the `Gen` and add productions). */
export function build(seed, installers, { units } = {}) {
  const g = new Gen(seed);
  for (const install of installers) install(g);
  const target = units ?? 12 + g.int(14);
  for (let tries = 0; g.units.length < target && tries < 500; tries++) {
    const makers = g.makers.filter((m) => !m.ok || m.ok(g));
    g.weighted(makers).make(g);
  }
  return g.finish();
}

/**
 * The source of the program. By default the names are as the generator spelled them (what Node runs as the bundle);
 * `top` and `local` rename the marked module-scope names and the unit's own names, as the fuzzer's splitter does.
 */
export function renderProgram(program, { top: renameTop = (n) => n, local: renameLocal = (n) => n } = {}) {
  const spell = (src) =>
    src.replace(TOP, (_, n) => renameTop(n)).replace(/\u0003([^\u0004]+)\u0004/g, (_, n) => renameLocal(n));
  const lines = [...program.imports, ...program.units.map((u) => spell(u.src))];
  if (program.exports.length > 0) lines.push(`export { ${program.exports.map(renameTop).join(", ")} };`);
  return lines.join("\n") + "\n";
}
