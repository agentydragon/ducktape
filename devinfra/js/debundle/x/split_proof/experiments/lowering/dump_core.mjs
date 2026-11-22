import { annotateLower, parseModule, resolveIdentifiers } from "../translation/scope_metadata.mjs";

/** The statements of the dump: the top-level statements, except that a `var`/`let`/`const` list of several
 * declarators becomes one declaration per declarator, in order, also under `export`. The declarators run in order
 * either way. Each statement has its node, the source range its identifiers lie in and standalone source text. */
function topLevelStatements(program, code) {
  return program.body.flatMap((statement) => {
    const exported = statement.type === "ExportNamedDeclaration" && statement.declaration;
    const list = exported || statement;
    if (
      list.type !== "VariableDeclaration" ||
      list.declarations.length < 2 ||
      !["var", "let", "const"].includes(list.kind)
    )
      return [
        {
          node: statement,
          start: statement.start,
          end: statement.end,
          src: code.slice(statement.start, statement.end),
        },
      ];
    return list.declarations.map((declarator) => {
      const declaration = { ...list, declarations: [declarator] };
      return {
        node: exported ? { ...statement, declaration } : declaration,
        start: declarator.start,
        end: declarator.end,
        src: `${exported ? "export " : ""}${list.kind} ${code.slice(declarator.start, declarator.end)};`,
      };
    });
  });
}

export function lowerDump(code, acorn, eslintScope) {
  return dumpParsed(parseModule(code, acorn.parse, eslintScope));
}

export function dumpParsed({ code, ast, scopes: sm }) {
  const stmts = topLevelStatements(ast, code);

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

  const meaning = resolveIdentifiers({ scopes: sm }, () => true);
  return {
    statements: stmts.map(({ node, src }, i) => ({
      // a new binder map for each statement
      ast: annotateLower(node, meaning),
      src,
      oracle: { free: [...oracle[i].free], eager: [...oracle[i].eager], ambiguous: [...oracle[i].ambiguous] },
    })),
  };
}
