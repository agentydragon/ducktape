// Lower a bundle to ESTree JSON for Lean, with an independent free-variable oracle (eslint-scope).
//
//   node dump.mjs <bundle.mjs> > bundle.json
//
// { "statements": [ { "ast": <ESTree, positions stripped>, "src": "<text>",
//                     "oracle": { "free": [...], "eager": [...], "ambiguous": [...] } } ] }
//
// oracle.free      names the statement mentions that resolve to module scope or to a global
// oracle.eager     those mentioned outside any function body
// oracle.ambiguous those mentioned only inside class field initializers / static blocks
import { readFileSync } from "node:fs";
import * as acorn from "acorn";
import * as eslintScope from "eslint-scope";

const [path] = process.argv.slice(2);
const code = readFileSync(path, "utf8");
const ast = acorn.parse(code, { ecmaVersion: "latest", sourceType: "module", ranges: true });
const sm = eslintScope.analyze(ast, { ecmaVersion: 2022, sourceType: "module" });
const stmts = ast.body;

const find = (pos) => {
  let lo = 0,
    hi = stmts.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (pos < stmts[mid].start) hi = mid - 1;
    else if (pos >= stmts[mid].end) lo = mid + 1;
    else return mid;
  }
  return -1;
};

const oracle = stmts.map(() => ({ free: new Set(), eager: new Set(), ambiguous: new Set() }));
for (const scope of sm.scopes) {
  for (const ref of scope.references) {
    if (ref.init) continue; // the declaration's own binding, not a use
    const i = find(ref.identifier.start);
    if (i < 0) continue;
    const v = ref.resolved;
    if (v && v.scope.type !== "global" && v.scope.type !== "module") continue; // bound inside the statement
    let inFn = false,
      inInit = false;
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

const drop = new Set(["start", "end", "loc", "range", "raw", "regex", "bigint", "sourceType"]);
function strip(k, v) {
  if (drop.has(k)) return undefined;
  if (k === "value" && this && this.type === "Literal") return undefined;
  return v;
}
const out = {
  statements: stmts.map((s, i) => ({
    ast: JSON.parse(JSON.stringify(s, strip)),
    src: code.slice(s.start, s.end),
    oracle: { free: [...oracle[i].free], eager: [...oracle[i].eager], ambiguous: [...oracle[i].ambiguous] },
  })),
};
process.stdout.write(JSON.stringify(out));
