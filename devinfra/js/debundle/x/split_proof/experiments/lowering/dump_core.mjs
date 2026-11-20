import { lowerStatements } from "../translation/scope_metadata.mjs";

export function lowerDump(code, acorn, eslintScope) {
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

  return {
    statements: lowerStatements(ast, sm, code).map((statement, i) => ({
      ...statement,
      oracle: { free: [...oracle[i].free], eager: [...oracle[i].eager], ambiguous: [...oracle[i].ambiguous] },
    })),
  };
}
