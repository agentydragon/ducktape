import { readFileSync } from "node:fs";
import { acorn } from "../translation/parser.mjs";
import { statementShape } from "./statement_shape.mjs";

for (const f of ["lodash", "acorn", "semver", "zod", "acorn.min", "zod.min"]) {
  const code = readFileSync(`bundles/${f}.mjs`, "utf8");
  const ast = acorn.parse(code, { ecmaVersion: "latest", sourceType: "module" });
  const hist = new Map();
  for (const statement of ast.body) {
    const shape = statementShape(statement);
    hist.set(shape, (hist.get(shape) ?? 0) + 1);
  }
  const top = [...hist]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 9)
    .map(([k, v]) => `${v} ${k}`);
  console.log(`${f.padEnd(10)} ${String(ast.body.length).padStart(5)} stmts | ${top.join(" ; ")}`);
}
