// The pairing tool: finds, for every unit of an output tree, the bundle unit it came from.
//
//   node pair.mjs <bundle.json> <output.json> --out cert.json
//
// Untrusted. `lean/Experiments/Translate.lean` re-checks every pair, so a wrong pairing is rejected there; a pairing
// that cannot be completed is reported here and left out of the certificate.
//
// Units with the same shape (identifiers by class, literals, property names) are candidates for each other. Copies of
// one helper share a shape; they are told apart by who uses them. Every unit gets a label that starts as its shape and
// is refined by the labels of the units it uses and of the units that use it (on both sides, with the output's names
// resolved through its imports). Units with equal labels pair up. Copies that nothing tells apart are interchangeable:
// one pair is fixed, the labels are refined again, and so on.
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { bodyUnits, resolveSource } from "./frontend.mjs";

/** The outside import at the root of a chain of plain member accesses. */
function vendorChain(node) {
  while (
    node.type === "MemberExpression" &&
    node.computed === false &&
    node.optional === false &&
    node.property.type === "Identifier" &&
    node.property.k === undefined
  )
    node = node.object;
  return node.type === "Identifier" && node.k === "V";
}

const isGlobalNamed = (node, name) => node.type === "Identifier" && node.k === "G" && node.name === name;
/** `new Worker("/p", …)` or `new Worker(new URL("/p", import.meta.url), …)` */
function isWorkerWithPath(node) {
  const first = node.arguments[0];
  if (!isGlobalNamed(node.callee, "Worker") || !first) return false;
  if (first.type === "Literal") return first.lit.startsWith("s:/");
  return (
    first.type === "NewExpression" &&
    isGlobalNamed(first.callee, "URL") &&
    first.arguments.length === 2 &&
    first.arguments[0].type === "Literal" &&
    first.arguments[0].lit.startsWith("s:/")
  );
}

function shapeKey(unit) {
  const parts = [];
  const locals = new Map();
  const metadata = new Set(["start", "end", "range", "loc", "raw", "sourceType", "sourceText"]);
  const walk = (node) => {
    if (Array.isArray(node)) {
      parts.push("[");
      node.forEach(walk);
      parts.push("]");
      return;
    }
    if (node === null || typeof node !== "object") {
      parts.push(JSON.stringify(node));
      return;
    }
    if (node.type === "Identifier") {
      if (node.k === "L") {
        if (!locals.has(node.b)) locals.set(node.b, locals.size);
        parts.push(`L${locals.get(node.b)}`);
      } else if (node.k === undefined) parts.push(`Name:${JSON.stringify(node.name)}`);
      else parts.push(`${node.k}${node.k === "G" ? `:${node.name}` : ""}${node.d ? "!" : ""}`);
      return;
    }
    if (node.type === "TemplateElement") {
      parts.push(`TemplateElement:${JSON.stringify([node.value.cooked, node.value.raw, node.tail])}`);
      return;
    }
    if (node.type === "MemberExpression" && vendorChain(node)) {
      parts.push("V");
      return;
    }
    if (node.type === "NewExpression" && isWorkerWithPath(node)) {
      parts.push("(NewWorker");
      walk(node.arguments.slice(1));
      parts.push(")");
      return;
    }
    if (node.type === "ImportExpression" && node.source.type === "Literal") {
      parts.push(`IMP:${node.source.lit.split("/").pop()}`);
      return;
    }
    parts.push(`(${node.type}`);
    for (const key of Object.keys(node).sort()) {
      if (key === "type" || metadata.has(key) || (node.type === "Property" && key === "shorthand")) continue;
      parts.push(`${key}=`);
      walk(node[key]);
    }
    parts.push(")");
  };
  walk(unit);
  return createHash("sha1").update(parts.join("|")).digest("hex");
}

/** Module-scope names a unit declares and uses. */
function topNames(unit) {
  const declared = new Set();
  const used = new Set();
  const walk = (node) => {
    if (Array.isArray(node)) node.forEach(walk);
    else if (node !== null && typeof node === "object") {
      if (node.type === "Identifier") {
        if (node.k === "T") (node.d === 1 ? declared : used).add(node.name);
        return;
      }
      for (const value of Object.values(node)) walk(value);
    }
  };
  walk(unit);
  return { declared, used };
}

const specName = (node) => (node.type === "Identifier" ? node.name : node.lit.slice(2));

function links(module, known) {
  const imports = new Map();
  const exports = new Map();
  for (const stmt of module.program.body) {
    if (stmt.type === "ImportDeclaration") {
      const from = resolveSource(known, module.path, specName(stmt.source));
      for (const spec of stmt.specifiers) {
        const imported =
          spec.type === "ImportDefaultSpecifier"
            ? "default"
            : spec.type === "ImportNamespaceSpecifier"
              ? "*"
              : specName(spec.imported);
        imports.set(spec.local.name, { from, imported });
      }
    } else if (stmt.type === "ExportNamedDeclaration" && !stmt.source) {
      if (stmt.declaration) {
        for (const name of topNames(stmt.declaration).declared) exports.set(name, name);
      } else {
        for (const spec of stmt.specifiers) exports.set(specName(spec.exported), spec.local.name);
      }
    }
  }
  return { imports, exports };
}

const [bundlePath, outputPath, ...rest] = process.argv.slice(2);
const outAt = rest.indexOf("--out");
if (!bundlePath || !outputPath || outAt < 0)
  throw new Error("usage: pair.mjs <bundle.json> <output.json> --out <cert.json>");
const bundle = JSON.parse(readFileSync(bundlePath, "utf8"));
const output = JSON.parse(readFileSync(outputPath, "utf8"));

// every unit gets a global id: the bundle's first, then the output's
const bundleUnits = bodyUnits(bundle.program);
const nB = bundleUnits.length;
const units = bundleUnits.map((ast) => ({ ast, side: "B" }));
output.modules.forEach((module, m) =>
  bodyUnits(module.program).forEach((ast, u) => units.push({ ast, side: "O", m, u, path: module.path }))
);
const names = units.map((unit) => topNames(unit.ast));
const label = units.map((unit) => shapeKey(unit.ast));

// who uses whom
const uses = units.map(() => []);
{
  const bundleDecl = new Map();
  for (let i = 0; i < nB; i++)
    for (const name of names[i].declared) bundleDecl.set(name, [...(bundleDecl.get(name) ?? []), i]);
  for (let i = 0; i < nB; i++)
    for (const name of names[i].used)
      for (const target of bundleDecl.get(name) ?? []) if (target !== i) uses[i].push(target);
  const index = new Map(output.modules.map((module, m) => [module.path, m]));
  const declOut = output.modules.map(() => new Map());
  for (let id = nB; id < units.length; id++) for (const name of names[id].declared) declOut[units[id].m].set(name, id);
  const known = new Set(index.keys());
  const linkage = output.modules.map((module) => links(module, known));
  const resolve = (m, name) => {
    if (declOut[m].has(name)) return declOut[m].get(name);
    const imp = linkage[m].imports.get(name);
    const src = imp && index.get(imp.from);
    return src === undefined ? undefined : declOut[src].get(linkage[src].exports.get(imp.imported));
  };
  for (let id = nB; id < units.length; id++) {
    for (const name of names[id].used) {
      const target = resolve(units[id].m, name);
      if (target !== undefined && target !== id) uses[id].push(target);
    }
  }
}
const usedBy = units.map(() => []);
uses.forEach((targets, id) => targets.forEach((t) => usedBy[t].push(id)));

const hash = (text) => createHash("sha1").update(text).digest("hex").slice(0, 16);
const classCount = () => new Set(label).size;
function refine() {
  for (let round = 0, before = classCount(); round < 20; round++) {
    const next = label.map((own, id) =>
      hash(
        `${own}|${uses[id]
          .map((t) => label[t])
          .sort()
          .join(",")}|${usedBy[id]
          .map((t) => label[t])
          .sort()
          .join(",")}`
      )
    );
    for (let id = 0; id < label.length; id++) label[id] = next[id];
    const after = classCount();
    if (after === before) return;
    before = after;
  }
}
refine();

const pairs = [];
const done = new Set();
let fixed = 0;
for (;;) {
  const classes = new Map();
  label.forEach((l, id) => {
    if (done.has(id)) return;
    const c = classes.get(l) ?? { B: [], O: [] };
    c[units[id].side].push(id);
    classes.set(l, c);
  });
  let ambiguous = null;
  for (const c of classes.values()) {
    if (c.B.length === 1 && c.O.length === 1) {
      pairs.push([units[c.O[0]].m, units[c.O[0]].u, c.B[0]]);
      done.add(c.B[0]).add(c.O[0]);
    } else if (!ambiguous && c.B.length > 0 && c.O.length > 0) ambiguous = c;
  }
  if (!ambiguous) break;
  // copies nothing tells apart: fix one pair (any pair does) and refine again
  const [b, o] = [ambiguous.B[0], ambiguous.O[0]];
  pairs.push([units[o].m, units[o].u, b]);
  done.add(b).add(o);
  label[b] = label[o] = `fixed${fixed++}`;
  refine();
}
const leftover = [];
for (let id = 0; id < units.length; id++)
  if (!done.has(id)) leftover.push(units[id].side === "B" ? `bundle ${id}` : `${units[id].path}#${units[id].u}`);
writeFileSync(rest[outAt + 1], JSON.stringify({ pairs }));
console.log(
  `paired ${pairs.length} of ${nB} bundle units; copies fixed by hand: ${fixed}; left over: ${leftover.length}`
);
if (leftover.length) console.log(`  e.g. ${leftover.slice(0, 6).join(", ")}`);
