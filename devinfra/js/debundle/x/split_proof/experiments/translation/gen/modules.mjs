// Family `modules`: the module forms a program can have besides its own export list. The program imports Node's built-in
// modules in every form (default, named, alias, namespace, side-effect), calls what they export, loads one with `import()`
// from a continuation and with `await import()` in an async function, reads `import.meta`, and registers callbacks with an
// `EventEmitter` that the imported code calls back. All of it is code the analysis cannot see into: a call into it is a call
// into the world, and a callback that reaches it escapes. The names an import binds are never marked, because the splitter
// repeats the import lines in every module it makes (`program.imports`).
//
// What is not here: `export default`, `export … from` and `export *`, which the translation checker refuses, and
// aliased exports, which the splitter would have to carry (an export is by name).
import { local } from "./core.mjs";

const FORMS = [
  { line: 'import nodePath from "node:path";', facility: "path" }, // import:default
  { line: 'import { basename, extname as extensionOf } from "node:path";', facility: "named" }, // import:named, import:alias
  { line: 'import * as osNs from "node:os";', facility: "os" }, // import:namespace
  { line: 'import "node:util";', facility: null }, // import:side-effect
  { line: 'import nodeUtil from "node:util";', facility: "util" },
  { line: 'import { createHash } from "node:crypto";', facility: "crypto" },
  { line: 'import { EventEmitter } from "node:events";', facility: "events" },
];

export function install(g) {
  const pick = (xs) => g.pick(xs);
  const N = (s, d = 2) => g.num(s, d);
  const S = (s, d = 2) => g.str(s, d);
  const has = new Set();
  for (const { line, facility } of FORMS) {
    if (!g.chance(0.6)) continue;
    g.imports.push(line);
    if (facility) has.add(facility);
  }
  if (g.imports.length === 0) {
    g.imports.push(FORMS[0].line);
    has.add("path");
  }
  const when = (facility) => (g, s) => has.has(facility);

  // ---- what the imports export, called with numbers and strings
  const num = g.prods.num;
  const str = g.prods.str;
  const calls = (facility, make) => ({ w: 2, ok: when(facility), make });
  num.push(
    calls("path", (g, s, d) =>
      pick([
        `nodePath.basename(${S(s, d)}).length`,
        `nodePath.join(${S(s, d)}, ${S(s, d)}).length`,
        `nodePath.normalize(${S(s, d)}).length`,
        `nodePath.resolve("/r", ${S(s, d)}).length`,
        `nodePath.relative("/a", "/a/b").length`,
        `nodePath.parse(${S(s, d)}).base.length`,
        "nodePath.sep.length",
      ])
    ),
    calls("named", (g, s, d) => pick([`basename(${S(s, d)}).length`, `extensionOf(${S(s, d)}).length`])),
    calls("os", () => "osNs.EOL.length"),
    calls("util", (g, s, d) =>
      pick([`nodeUtil.format("%d|%s", ${N(s, d)}, ${S(s, d)}).length`, `nodeUtil.inspect({ a: ${N(s, d)} }).length`])
    ),
    calls("crypto", (g, s, d) => `createHash(${pick(['"sha1"', '"md5"'])}).update(${S(s, d)}).digest("hex").length`)
  );
  str.push(
    calls("path", (g, s, d) => pick([`nodePath.basename(${S(s, d)})`, `nodePath.extname(${S(s, d)})`])),
    calls("util", (g, s, d) => `nodeUtil.format("%s", ${S(s, d)})`),
    calls("crypto", (g, s, d) => `createHash("sha256").update(${S(s, d)}).digest("hex").slice(0, 8)`)
  );
  g.prods.bool.push(
    calls("path", (g, s, d) => `nodePath.isAbsolute(${S(s, d)})`),
    calls("util", (g, s, d) => {
      const arrays = g.arrays(s, "num");
      return arrays.length > 1
        ? `nodeUtil.isDeepStrictEqual(${g.ref(pick(arrays))}, ${g.ref(pick(arrays))})`
        : `nodeUtil.isDeepStrictEqual([${N(s, d)}], [${N(s, d)}])`;
    })
  );

  // ---- `import.meta` and `import()`. The checker refuses a tree in which a unit reads `import.meta` (it is module-relative), so a
  // program that does is rare: a split is checked for the rest.
  const meta = g.chance(0.1);
  const onMeta = () => meta;
  num.push({ w: 2, leaf: true, ok: onMeta, make: () => 'import.meta.url.indexOf(":")' });
  str.push({ w: 2, leaf: true, ok: onMeta, make: () => "import.meta.url.slice(0, 5)" });
  g.prods.bool.push({ w: 2, leaf: true, ok: onMeta, make: () => 'import.meta.url.startsWith("file:")' });
  num.push({
    w: 2,
    ok: (g, s) => s.fn?.async,
    make: () => pick(['(await import("node:path")).sep.length', '(await import("node:os")).EOL.length']),
  });
  const st = g.stmts;
  st.push({
    w: 2,
    ok: (g, s) => s.nest > 0,
    make: (g, s) => {
      g.late = true;
      const m = { name: g.fresh("l"), scope: "local", type: "ns", mutable: false };
      const sc = g.callback(s, [m], { ret: "void" });
      return `import(${pick(['"node:path"', '"node:os"', '"node:util"'])}).then((${local(m.name)}) => ${g.block(sc, 1)});`;
    },
  });
  g.observers.ns = () => null;

  // ---- an EventEmitter whose handlers are fixed where it is made, so that what `emit` runs is known
  const emitters = (s) => g.refs(s, (b) => b.type?.t === "emitter");
  g.makers.push({
    name: "event emitter",
    w: 3,
    ok: () => has.has("events"),
    make: (g) => {
      const events = {};
      const parts = [];
      for (const name of ["a", "b"].slice(0, 1 + g.int(2))) {
        const x = { name: g.fresh("l"), scope: "local", type: "num", mutable: false };
        const sc = g.callback(g.topScope(), [x], { ret: "void" });
        events[name] = sc.writes;
        parts.push(
          `${pick(["on", "once", "prependListener"])}("${name}", (${local(x.name)}) => ${g.block(sc, 1 + g.int(2))})`
        );
      }
      const e = g.binding("n", { t: "emitter", events }, { kind: "const" });
      g.add(
        "event emitter",
        `const ${g.ref(e)} = (() => { const e = new EventEmitter(); ${parts.map((p) => `e.${p};`).join(" ")} return e; })();`,
        [e]
      );
    },
  });
  const usable = (s, writes) => ![...writes].some((n) => s.frozen.has(n));
  const ready = (s) => emitters(s).filter((e) => Object.values(e.type.events).every((w) => usable(s, w)));
  const emit = (s, d) => {
    const e = pick(ready(s));
    const name = pick(Object.keys(e.type.events));
    for (const w of Object.values(e.type.events)) for (const n of w) s.writes.add(n);
    return `${g.ref(e)}.emit("${name}", ${N(s, d)})`;
  };
  num.push({ w: 3, ok: (g, s) => ready(s).length > 0, make: (g, s, d) => `(${emit(s, d)} ? 1 : 0)` });
  st.push({ w: 3, ok: (g, s) => ready(s).length > 0, make: (g, s) => `${emit(s, 1)};` });
  g.observers.emitter = () => null;
}
