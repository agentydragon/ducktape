import { readFileSync } from "node:fs";
import * as acorn from "acorn";
const shape = (s) => {
  if (s.type === "VariableDeclaration") {
    const k = s.declarations.map((d) => d.init?.type ?? "none");
    return `${s.kind} ${[...new Set(k)].join("|")}`;
  }
  if (s.type === "ExpressionStatement") {
    const e = s.expression;
    if (e.type === "AssignmentExpression") return `assign ${e.left.type}=${e.right.type}`;
    if (e.type === "CallExpression") return `call ${e.callee.type}`;
    return e.type;
  }
  return s.type;
};
for (const f of ["lodash", "acorn", "semver", "zod", "acorn.min", "zod.min"]) {
  const code = readFileSync(`bundles/${f}.mjs`, "utf8");
  const ast = acorn.parse(code, { ecmaVersion: "latest", sourceType: "module" });
  const hist = new Map();
  for (const s of ast.body) hist.set(shape(s), (hist.get(shape(s)) ?? 0) + 1);
  const top = [...hist]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 9)
    .map(([k, v]) => `${v} ${k}`);
  console.log(`${f.padEnd(10)} ${String(ast.body.length).padStart(5)} stmts | ${top.join(" ; ")}`);
}
