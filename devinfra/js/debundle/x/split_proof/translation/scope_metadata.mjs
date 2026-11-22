// Shared scope facts for the translation frontend and LowerSplit's raw ESTree.
// The parser and eslint-scope remain trusted; this module does not resolve names itself.

/** Enumerate units for the external JS model and diagnostics. Array indices are source ordinals,
 * including imports and export lists. Keep the original identifier nodes for scope lookup. */
export function topLevelStatements(program, code) {
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

/** The ESTree and (when requested) scope manager for one JavaScript module.
 * Scope references point to identifier objects in this exact tree. */
export function parseModule(code, parse, analyzer = null, scopeOptions = {}) {
  const ast = parse(code, { ecmaVersion: "latest", sourceType: "module", ranges: true });
  const scopes = analyzer?.analyze(ast, { ecmaVersion: 2022, sourceType: "module", ...scopeOptions }) ?? null;
  return { code, ast, scopes };
}

/** Identifier node -> what it means. `isExternal(source)` identifies external imports. */
export function resolveIdentifiers({ scopes }, isExternal) {
  const meaning = new Map();
  const classOf = (variable) => {
    if (!variable) return "G";
    const definition = variable.defs[0];
    if (definition?.type === "ImportBinding" && isExternal(String(definition.parent.source.value))) return "V";
    return variable.scope.type === "module" ? "T" : variable.scope.type === "global" ? "G" : "L";
  };
  for (const scope of scopes.scopes) {
    for (const variable of scope.variables) {
      const declaresModuleBinding =
        variable.scope.type === "module" && variable.defs.some((d) => d.type !== "ImportBinding");
      for (const id of variable.identifiers)
        if (!meaning.has(id)) meaning.set(id, { cls: classOf(variable), variable, declares: declaresModuleBinding });
    }
    for (const ref of scope.references)
      if (!meaning.has(ref.identifier))
        meaning.set(ref.identifier, { cls: classOf(ref.resolved), variable: ref.resolved, declares: false });
  }
  return meaning;
}

export function literalKey(node) {
  if (node.regex) return `r:${node.regex.pattern}/${node.regex.flags}`;
  if (node.bigint !== undefined) return `bi:${node.bigint}`;
  if (node.value === null) return "null";
  return `${{ string: "s", number: "n", boolean: "b" }[typeof node.value]}:${node.value}`;
}

const POSITION_FIELDS = new Set(["start", "end", "loc", "range"]);
const LITERAL_DROP = new Set(["value", "raw", "regex", "bigint"]);

function annotate(node, meaning, ranged, binders) {
  if (Array.isArray(node)) return node.map((child) => annotate(child, meaning, ranged, binders));
  if (node === null || typeof node !== "object") return node;
  const keepRange = ranged.has(node);
  const out = keepRange ? { start: node.start, end: node.end } : {};
  for (const [key, value] of Object.entries(node)) {
    if (!POSITION_FIELDS.has(key) && !(node.type === "Literal" && LITERAL_DROP.has(key)))
      out[key] = annotate(value, meaning, ranged, binders);
  }
  if (node.type === "Identifier") {
    const m = meaning.get(node);
    if (m) {
      out.k = m.cls;
      if (m.cls === "L") {
        if (!binders.has(m.variable)) binders.set(m.variable, binders.size);
        out.b = binders.get(m.variable);
      }
      if (m.declares) out.d = 1;
    }
  }
  if (node.type === "Literal") out.lit = literalKey(node);
  return out;
}

/** Preserve the Program structure with scope/literal annotations. Acorn's UTF-16 ranges
 * and the original source let Lean recover text for each statement or declarator. */
export function annotateProgram({ code, ast }, meaning) {
  const ranged = new Set(ast.body);
  for (const statement of ast.body) {
    const declaration = statement.type === "ExportNamedDeclaration" ? statement.declaration : statement;
    if (declaration?.type === "VariableDeclaration") {
      for (const declarator of declaration.declarations) ranged.add(declarator);
    }
  }
  return { ...annotate(ast, meaning, ranged, new Map()), sourceText: code };
}
