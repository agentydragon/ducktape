// How many top-level units of a bundle only allocate? A unit built from literals, names, object and array literals,
// function and arrow expressions and plain classes runs no code but its own and reads through no object: the shape of
// the first footprint rule to prove against a model ([`PLAN.md`](../../PLAN.md) § Direction). A count by syntax.
//
//   node allocate_only_census.mjs <bundle.json> <footprints.jsonl>
//
// `bundle.json` is the output of `frontend.mjs bundle`, `footprints.jsonl` that of `lower` on it: the count says how
// many of the units that only allocate the analysis calls an effect (it should be none).
import fs from "node:fs";
const b = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const fps = fs
  .readFileSync(process.argv[3], "utf8")
  .split("\n")
  .filter(Boolean)
  .map((l) => JSON.parse(l));
const FN = new Set(["FunctionExpression", "ArrowFunctionExpression", "FunctionDeclaration"]);
const why = new Map();
// returns undefined when `n` only allocates, else the first node type that does more
function more(n) {
  if (!n || typeof n !== "object") return undefined;
  if (Array.isArray(n)) {
    for (const x of n) {
      const r = more(x);
      if (r) return r;
    }
    return undefined;
  }
  switch (n.type) {
    case "Literal":
    case "Identifier":
    case "Name":
    case "ThisExpression":
      return undefined;
    case "FunctionExpression":
    case "ArrowFunctionExpression":
    case "FunctionDeclaration":
      return undefined; // the body does not run
    case "TemplateLiteral":
      return n.expressions.length ? "TemplateLiteral with expressions" : undefined;
    case "ArrayExpression":
      return n.elements.some((e) => e && e.type === "SpreadElement") ? "SpreadElement" : more(n.elements);
    case "ObjectExpression":
      for (const p of n.properties) {
        if (p.type === "SpreadElement") return "SpreadElement";
        if (p.computed) {
          const r = more(p.key);
          if (r) return r;
        }
        if (p.kind === "init" || p.kind === undefined) {
          const r = more(p.value);
          if (r) return r;
        }
      }
      return undefined;
    case "VariableDeclaration":
      for (const d of n.declarations) {
        if (d.id.type !== "Identifier" && d.id.type !== "Name") return "destructuring";
        const r = more(d.init);
        if (r) return r;
      }
      return undefined;
    case "ClassDeclaration":
    case "ClassExpression":
      if (n.superClass) return "class extends";
      for (const m of n.body.body) {
        if (m.type === "StaticBlock") return "static block";
        if (m.computed) return "computed class key";
        if (m.type === "PropertyDefinition" && m.static && m.value) {
          const r = more(m.value);
          if (r) return `static field: ${r}`;
        }
      }
      return undefined;
    case "UnaryExpression":
      return ["void", "!"].includes(n.operator) && n.argument.type === "Literal"
        ? undefined
        : n.operator === "-" && n.argument.type === "Literal"
          ? undefined
          : `UnaryExpression ${n.operator}`;
    case "ExportNamedDeclaration":
      return n.declaration ? more(n.declaration) : undefined;
    default:
      return n.type;
  }
}
let alloc = 0,
  fn = 0,
  cls = 0,
  vars = 0,
  allocEffect = 0,
  notAllocPure = 0;
b.units.forEach((u, i) => {
  const r = more(u);
  if (!r) {
    alloc++;
    if (u.type === "FunctionDeclaration") fn++;
    else if (u.type === "ClassDeclaration") cls++;
    else vars++;
    if (fps[i].effect) allocEffect++;
  } else {
    why.set(r, (why.get(r) ?? 0) + 1);
    if (!fps[i].effect) notAllocPure++;
  }
});
const n = b.units.length;
console.log(
  `${n} units; allocate only: ${alloc} (${((100 * alloc) / n).toFixed(1)}%): ${fn} function declarations, ${cls} plain classes, ${vars} variable declarations`
);
console.log(
  `of those, the analysis today calls ${allocEffect} an effect; units that do more but the analysis calls no effect: ${notAllocPure}`
);
console.log(
  "first thing that does more, by count:",
  [...why]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 12)
    .map(([k, v]) => `${k} ${v}`)
    .join("; ")
);
