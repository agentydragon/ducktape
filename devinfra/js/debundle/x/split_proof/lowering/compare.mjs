// Run the Lean footprint analysis on dumped bundles and compare with eslint-scope (and extract.mjs).
//
//   node compare.mjs <bundle.json>... [--example]
import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { basename, resolve } from "node:path";
import { acorn, eslintScope } from "../translation/bazel_parser.mjs";
import { footprints } from "../extract/extract.mjs";
import { parseModule, topLevelStatements } from "../translation/scope_metadata.mjs";

const LOWER = resolve(process.env.LOWER ?? new URL("../lean/.lake/build/bin/lower", import.meta.url).pathname);
const files = process.argv.slice(2).filter((a) => !a.startsWith("--"));
const set = (a) => new Set(a);
const missing = (need, have) => [...need].filter((x) => !have.has(x));

/** Independent oracle from eslint-scope's resolved references and the JS model's unit boundaries. */
function scopeFootprints(scopes, units) {
  const find = (pos) => {
    let lo = 0;
    let hi = units.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (pos < units[mid].start) hi = mid - 1;
      else if (pos >= units[mid].end) lo = mid + 1;
      else return mid;
    }
    return -1;
  };
  const oracle = units.map(() => ({ free: new Set(), eager: new Set(), ambiguous: new Set() }));
  for (const scope of scopes.scopes) {
    for (const ref of scope.references) {
      if (ref.init) continue;
      const i = find(ref.identifier.start);
      if (i < 0) continue;
      const v = ref.resolved;
      if (v && v.scope.type !== "global" && v.scope.type !== "module") continue;
      let inFn = false;
      let inInit = false;
      for (let s = ref.from; s && s.type !== "module" && s.type !== "global"; s = s.upper) {
        if (s.type === "function") inFn = true;
        if (s.type === "class-field-initializer" || s.type === "class-static-block") inInit = true;
      }
      const name = ref.identifier.name;
      oracle[i].free.add(name);
      if (!inFn && !inInit) oracle[i].eager.add(name);
      if (!inFn && inInit) oracle[i].ambiguous.add(name);
    }
  }
  return oracle;
}

const shape = (s) => {
  if (s.type === "VariableDeclaration")
    return `${s.kind} ${[...new Set(s.declarations.map((d) => d.init?.type ?? "none"))].join("|")}`;
  if (s.type === "ExpressionStatement") {
    const e = s.expression;
    if (e.type === "AssignmentExpression") return `assign ${e.left.type}=${e.right.type}`;
    if (e.type === "CallExpression") return `call ${e.callee.type}`;
    return e.type;
  }
  return s.type;
};

function closureSizes(fps) {
  const owners = new Map();
  fps.forEach((f, i) => f.defs.forEach((d) => owners.set(d, [...(owners.get(d) ?? []), i])));
  return fps.map((f) => {
    if (f.hoisted) return 0;
    const seen = new Set(f.eager),
      queue = [...f.eager];
    while (queue.length) {
      const u = queue.pop();
      for (const j of owners.get(u) ?? [])
        for (const w of [...fps[j].eager, ...fps[j].lazy])
          if (!seen.has(w)) {
            seen.add(w);
            queue.push(w);
          }
    }
    return seen.size;
  });
}

/** Render the split declarator snippets used by the legacy comparison report. */
function unitSource({ node, start, end }, source) {
  const declaration = node.type === "ExportNamedDeclaration" ? node.declaration : node;
  const splitDeclarator = start !== node.start || end !== node.end;
  if (splitDeclarator && declaration?.type === "VariableDeclaration") {
    const prefix = node.type === "ExportNamedDeclaration" ? "export " : "";
    return `${prefix}${declaration.kind} ${source.slice(start, end)};`;
  }
  return source.slice(start, end);
}

const median = (a) => {
  const s = [...a].sort((x, y) => x - y);
  return s.length ? s[s.length >> 1] : 0;
};

const rows = [];
for (const file of files) {
  const dump = JSON.parse(readFileSync(file, "utf8"));
  const parsed = parseModule(dump.sourceText, acorn.parse, eslintScope);
  const units = topLevelStatements(parsed.ast);
  const oracle = scopeFootprints(parsed.scopes, units);
  const t0 = performance.now();
  const out = execFileSync(LOWER, [file], { maxBuffer: 1 << 30, encoding: "utf8" });
  const ms = performance.now() - t0;
  const fps = out
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
  assert.equal(fps.length, units.length, file);
  const n = fps.length;
  let unsound = 0,
    unsoundEager = 0,
    exact = 0,
    extraNames = 0,
    withFallback = 0,
    assigns = 0;
  const examples = [];
  const fallbackTypes = new Map();
  const effShapes = new Map();
  units.forEach((unit, i) => {
    const { node } = unit;
    const src = unitSource(unit, parsed.code);
    const f = fps[i];
    const lean = set([...f.rawEager, ...f.rawLazy]);
    const { free, eager, ambiguous } = oracle[i];
    const miss = missing(free, lean);
    if (miss.length) {
      unsound++;
      if (examples.length < 3)
        examples.push(`stmt ${i} (${node.type}): lean misses ${miss.slice(0, 4)} :: ${src.slice(0, 60)}`);
    }
    const missE = missing(eager, set(f.rawEager)).filter((x) => !ambiguous.has(x));
    if (missE.length) {
      unsoundEager++;
      if (examples.length < 3)
        examples.push(`stmt ${i}: lean calls eager ${missE.slice(0, 4)} lazy :: ${src.slice(0, 60)}`);
    }
    const extra = missing(lean, free);
    if (!miss.length && !extra.length) exact++;
    extraNames += extra.length;
    if (f.fallback.length) {
      withFallback++;
      f.fallback.forEach((t) => fallbackTypes.set(t, (fallbackTypes.get(t) ?? 0) + 1));
    }
    if (f.assigns.length) assigns++;
    if (f.effect) effShapes.set(shape(node), (effShapes.get(shape(node)) ?? 0) + 1);
  });
  const eff = fps.filter((f) => f.effect).length;
  const hoisted = fps.filter((f) => f.hoisted).length;
  const cl = closureSizes(fps);
  rows.push({
    name: basename(file, ".json"),
    n,
    eff,
    inert: n - eff,
    hoisted,
    unsound,
    unsoundEager,
    exactPct: ((100 * exact) / n).toFixed(1),
    extraNames,
    withFallback,
    assigns,
    ms: ms.toFixed(0),
    medClosure: median(cl),
    maxClosure: Math.max(...cl),
  });
  console.log(
    `\n${basename(file)}: top effect shapes: ${[...effShapes]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5)
      .map(([k, v]) => `${v} ${k}`)
      .join(" ; ")}`
  );
  if (fallbackTypes.size)
    console.log(`  fallback node types: ${[...fallbackTypes].map(([k, v]) => `${k}×${v}`).join(", ")}`);
  examples.forEach((e) => console.log("  !!", e));
}
console.table(
  rows.map(
    ({
      name,
      n,
      inert,
      eff,
      hoisted,
      unsound,
      unsoundEager,
      exactPct,
      withFallback,
      assigns,
      ms,
      medClosure,
      maxClosure,
    }) => ({
      bundle: name,
      stmts: n,
      inert,
      effect: eff,
      hoistedFn: hoisted,
      "unsound(free)": unsound,
      "unsound(eager)": unsoundEager,
      "exact%": exactPct,
      "w/ fallback": withFallback,
      "assigns top-level": assigns,
      "lean ms": ms,
      "closure med": medClosure,
      "closure max": maxClosure,
    })
  )
);

if (process.argv.includes("--example")) {
  const dump = JSON.parse(readFileSync(files[0], "utf8"));
  const mine = footprints(
    topLevelStatements(dump)
      .map((unit) => unitSource(unit, dump.sourceText))
      .join("\n")
  );
  const fps = execFileSync(LOWER, [files[0]], { encoding: "utf8" })
    .trim()
    .split("\n")
    .map((l) => JSON.parse(l));
  let diffs = 0;
  mine.forEach((m, i) => {
    const f = fps[i];
    const same = (a, b) => JSON.stringify([...new Set(a)].sort()) === JSON.stringify([...new Set(b)].sort());
    if (!(
      same(m.defs, f.defs) &&
      same(m.eager, f.eager) &&
      same(m.lazy, f.lazy) &&
      m.hoisted === f.hoisted &&
      m.effect === f.effect
    )) {
      diffs++;
      console.log(
        "DIFF",
        i,
        m.src,
        "\n  js  :",
        JSON.stringify({ ...m, src: undefined }),
        "\n  lean:",
        JSON.stringify(f)
      );
    }
  });
  console.log(
    `\nextract.mjs vs Lean on the example: ${mine.length - diffs}/${mine.length} statements identical footprints`
  );
}
