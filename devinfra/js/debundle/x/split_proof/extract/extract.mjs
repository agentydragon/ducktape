// Front end (trusted): bundle + spec + controls  ->  lean/EsmSplit/Instance.lean
//
//   node extract.mjs <bundle.mjs> <spec.json> [--variant <spec.json>]... [--control <spec.json>]... > Instance.lean
//
// A variant is another spec that must pass. A control is a spec that must fail the check named
// in its `fails` field.
//
// For every top-level statement it computes the footprint the Lean side reasons about:
//   defs     names the statement declares
//   eager    names read while the statement runs
//   lazy     names read only inside function bodies created by the statement
//            (for a statement with effects, lazy names are also eager: a call may invoke them)
//   hoisted  function declaration
//   effect   anything that is not obviously inert (calls, member access, operators, ...)
// Mistakes are only allowed in the safe direction (extra reads, extra effects).
// Syntax this file does not model is rejected, never guessed.

import { readFileSync } from "node:fs";
import { basename } from "node:path";
import { pathToFileURL } from "node:url";
import * as acorn from "acorn";

let code; // source text being analyzed (for error messages and statement slices)

const unsupported = (node, what) => {
  throw new Error(`unsupported ${what} at offset ${node.start}: ${code.slice(node.start, node.start + 50)}`);
};

const isBound = (scope, name) => {
  for (let s = scope; s; s = s.parent) if (s.names.has(name)) return true;
  return false;
};

// ctx = { eager: Set, lazy: Set, inert: boolean }. `inFn` is true inside function bodies.
const impure = (ctx, inFn) => {
  if (!inFn) ctx.inert = false;
};

function refer(ctx, scope, node, inFn) {
  if (node.name === "arguments" || node.name === "eval") unsupported(node, `\`${node.name}\``);
  if (!isBound(scope, node.name)) (inFn ? ctx.lazy : ctx.eager).add(node.name);
}

function target(t, ctx, scope, inFn) {
  if (t.type === "Identifier") {
    if (!isBound(scope, t.name)) unsupported(t, `assignment to non-local binding ${t.name}`);
    return;
  }
  if (t.type === "MemberExpression") return expr(t, ctx, scope, inFn);
  unsupported(t, `assignment target ${t.type}`);
}

function expr(n, ctx, scope, inFn) {
  switch (n.type) {
    case "Literal":
    case "ThisExpression":
      return;
    case "Identifier":
      return refer(ctx, scope, n, inFn);
    case "TemplateLiteral":
      if (n.expressions.length) impure(ctx, inFn);
      return n.expressions.forEach((e) => expr(e, ctx, scope, inFn));
    case "ArrayExpression":
      for (const e of n.elements) {
        if (!e) continue;
        if (e.type === "SpreadElement") {
          impure(ctx, inFn);
          expr(e.argument, ctx, scope, inFn);
        } else expr(e, ctx, scope, inFn);
      }
      return;
    case "ObjectExpression":
      for (const p of n.properties) {
        if (p.type === "SpreadElement") {
          impure(ctx, inFn);
          expr(p.argument, ctx, scope, inFn);
          continue;
        }
        if (p.computed) {
          impure(ctx, inFn);
          expr(p.key, ctx, scope, inFn);
        }
        expr(p.value, ctx, scope, inFn);
      }
      return;
    case "ArrowFunctionExpression":
    case "FunctionExpression":
      return fn(n, ctx, scope);
    case "UnaryExpression":
      impure(ctx, inFn);
      return expr(n.argument, ctx, scope, inFn);
    case "BinaryExpression":
    case "LogicalExpression":
      impure(ctx, inFn);
      expr(n.left, ctx, scope, inFn);
      return expr(n.right, ctx, scope, inFn);
    case "ConditionalExpression":
      impure(ctx, inFn);
      expr(n.test, ctx, scope, inFn);
      expr(n.consequent, ctx, scope, inFn);
      return expr(n.alternate, ctx, scope, inFn);
    case "SequenceExpression":
      impure(ctx, inFn);
      return n.expressions.forEach((e) => expr(e, ctx, scope, inFn));
    case "MemberExpression":
      impure(ctx, inFn);
      expr(n.object, ctx, scope, inFn);
      if (n.computed) expr(n.property, ctx, scope, inFn);
      return;
    case "ChainExpression":
      return expr(n.expression, ctx, scope, inFn);
    case "CallExpression":
    case "NewExpression":
      impure(ctx, inFn);
      expr(n.callee, ctx, scope, inFn);
      for (const a of n.arguments) expr(a.type === "SpreadElement" ? a.argument : a, ctx, scope, inFn);
      return;
    case "TaggedTemplateExpression":
      impure(ctx, inFn);
      expr(n.tag, ctx, scope, inFn);
      return expr(n.quasi, ctx, scope, inFn);
    case "AssignmentExpression":
      impure(ctx, inFn);
      target(n.left, ctx, scope, inFn);
      return expr(n.right, ctx, scope, inFn);
    case "UpdateExpression":
      impure(ctx, inFn);
      return target(n.argument, ctx, scope, inFn);
    default:
      unsupported(n, n.type);
  }
}

// Function bodies only run when called, so every read inside is lazy.
function fn(n, ctx, scope) {
  if (n.async || n.generator) unsupported(n, "async or generator function");
  const names = new Set();
  if (n.type === "FunctionExpression" && n.id) names.add(n.id.name);
  for (const p of n.params) {
    if (p.type !== "Identifier") unsupported(p, "parameter pattern");
    names.add(p.name);
  }
  const inner = { parent: scope, names };
  if (n.body.type === "BlockStatement") block(n.body.body, ctx, inner);
  else expr(n.body, ctx, inner, true);
}

function block(stmts, ctx, scope) {
  const names = new Set();
  for (const s of stmts) {
    if (s.type === "VariableDeclaration") {
      for (const d of s.declarations) {
        if (d.id.type !== "Identifier") unsupported(d.id, "destructuring");
        names.add(d.id.name);
      }
    } else if (s.type === "FunctionDeclaration") names.add(s.id.name);
  }
  const inner = { parent: scope, names };
  for (const s of stmts) stmt(s, ctx, inner);
}

function stmt(s, ctx, scope) {
  switch (s.type) {
    case "ExpressionStatement":
      return expr(s.expression, ctx, scope, true);
    case "ReturnStatement":
    case "ThrowStatement":
      return s.argument && expr(s.argument, ctx, scope, true);
    case "VariableDeclaration":
      return s.declarations.forEach((d) => d.init && expr(d.init, ctx, scope, true));
    case "FunctionDeclaration":
      return fn(s, ctx, scope);
    case "IfStatement":
      expr(s.test, ctx, scope, true);
      stmt(s.consequent, ctx, scope);
      return s.alternate && stmt(s.alternate, ctx, scope);
    case "BlockStatement":
      return block(s.body, ctx, scope);
    default:
      unsupported(s, s.type);
  }
}

function topLevel(s) {
  const ctx = { eager: new Set(), lazy: new Set(), inert: true };
  const defs = [];
  let hoisted = false;
  switch (s.type) {
    case "FunctionDeclaration":
      defs.push(s.id.name);
      hoisted = true;
      fn(s, ctx, null);
      break;
    case "VariableDeclaration":
      for (const d of s.declarations) {
        if (d.id.type !== "Identifier") unsupported(d.id, "destructuring");
        defs.push(d.id.name);
        if (d.init) expr(d.init, ctx, null, false);
      }
      break;
    case "ExpressionStatement":
      expr(s.expression, ctx, null, false);
      break;
    default:
      unsupported(s, s.type);
  }
  return {
    src: code.slice(s.start, s.end),
    defs,
    eager: [...ctx.eager],
    lazy: [...ctx.lazy],
    hoisted,
    effect: !ctx.inert,
  };
}

// ---- Lean output ----

const q = (s) => JSON.stringify(s);
const list = (xs) => `[${xs.map(q).join(", ")}]`;

function leanStmt(st) {
  const f = [`src := ${q(st.src)}`];
  if (st.defs.length) f.push(`defs := ${list(st.defs)}`);
  if (st.eager.length) f.push(`eager := ${list(st.eager)}`);
  if (st.lazy.length) f.push(`lazy := ${list(st.lazy)}`);
  if (st.hoisted) f.push("hoisted := true");
  if (st.effect) f.push("effect := true");
  return `  { ${f.join(", ")} }`;
}

const selector = (s) => {
  if (!s.startsWith("#")) return `.name ${q(s)}`;
  if (!/^#\d+$/.test(s)) throw new Error(`bad selector ${s}`);
  return `.stmt ${s.slice(1)}`;
};

function leanSpec(id, spec) {
  const mods = Object.entries(spec.modules)
    .map(([m, sels]) => `    (${q(m)}, [${sels.map(selector).join(", ")}])`)
    .join(",\n");
  return `def ${id} : Spec := {\n  entry := ${q(spec.entry)}\n  modules := [\n${mods}\n  ]\n}`;
}

/** Footprints of the top-level statements of a flat bundle. */
export function footprints(source) {
  code = source;
  const ast = acorn.parse(code, { ecmaVersion: "latest", sourceType: "module" });
  const body = ast.body.map((s) => {
    if (/^(Import|Export)/.test(s.type)) unsupported(s, "import/export in a flat bundle");
    return topLevel(s);
  });
  // Reading a name nobody declares can throw ReferenceError (or hit a getter on the global object),
  // so such a statement is ordered like an effect: reordering must not move the throw.
  const declared = new Set(body.flatMap((s) => s.defs));
  for (const st of body) if (st.eager.some((n) => !declared.has(n))) st.effect = true;
  // A statement that calls anything may call any closure it mentions (a callback passed to
  // `map`, an immediately invoked function), so everything it mentions is read while it runs.
  for (const st of body) if (st.effect) st.eager = [...new Set([...st.eager, ...st.lazy])];
  return body;
}

export const leanStmtText = (st) => leanStmt(st);

function cli() {
  const [bundlePath, specPath, ...rest] = process.argv.slice(2);
  const variantPaths = [];
  const controlPaths = [];
  for (let i = 0; i < rest.length; i += 2) {
    if (rest[i] === "--variant") variantPaths.push(rest[i + 1]);
    else if (rest[i] === "--control") controlPaths.push(rest[i + 1]);
    else throw new Error(`bad argument ${rest[i]}`);
  }
  const body = footprints(readFileSync(bundlePath, "utf8"));
  const readJson = (p) => JSON.parse(readFileSync(p, "utf8"));
  const specs = readJson(specPath);
  const variants = variantPaths.map((p) => ({ id: basename(p, ".json"), ...readJson(p) }));
  const controls = controlPaths.map((p) => ({ id: basename(p, ".json"), ...readJson(p) }));
  const CHECKS = new Set(["Faithful", "Linkable", "Hygienic", "Permutes", "OrderOk"]);

  const out = [];
  out.push(
    `-- GENERATED by extract/extract.mjs from the bundle, the spec, the variants and the controls. Do not edit.`
  );
  out.push("import EsmSplit.Split\nimport EsmSplit.Correct\n\nopen EsmSplit\n\nnamespace Example\n");
  out.push(
    `/-- Footprints of the ${body.length} top-level statements of ${basename(bundlePath)}. -/\ndef bundle : Bundle := [\n${body.map(leanStmt).join(",\n")}\n]\n`
  );
  out.push(`${leanSpec("spec", specs)}\n`);
  out.push("def plan : Plan := mkPlan bundle spec\n");
  out.push("theorem plan_valid : Valid bundle spec plan := by decide +kernel\n");
  out.push("theorem plan_correct : Correct bundle plan := split_correct plan_valid\n");
  for (const v of variants) {
    out.push(`/-- ${v.why} -/\n${leanSpec(`variant_${v.id}`, v)}\n`);
    out.push(
      `theorem variant_${v.id}_valid : Valid bundle variant_${v.id} (mkPlan bundle variant_${v.id}) := by decide +kernel\n`
    );
  }
  out.push(
    `def specVariants : List (String × Spec) := [${variants.map((v) => `(${q(v.id)}, variant_${v.id})`).join(", ")}]\n`
  );
  for (const c of controls) {
    if (!CHECKS.has(c.fails)) throw new Error(`control ${c.id}: unknown check ${c.fails}`);
    out.push(`/-- ${c.why} -/\n${leanSpec(`control_${c.id}`, c)}\n`);
    const args = c.fails === "Faithful" ? `bundle control_${c.id}` : "bundle";
    out.push(
      `theorem control_${c.id}_rejected : ¬ ${c.fails} ${args} (mkPlan bundle control_${c.id}) := by decide +kernel\n`
    );
  }
  out.push(
    `def specControls : List (String × Spec) := [${controls.map((c) => `(${q(c.id)}, control_${c.id})`).join(", ")}]\n`
  );
  out.push("end Example");
  process.stdout.write(out.join("\n") + "\n");
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) cli();
