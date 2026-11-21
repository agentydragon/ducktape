// Shared scope facts for the translation frontend and LowerSplit's raw ESTree.
// The parser and eslint-scope remain trusted; this module does not resolve names itself.

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

const LOWER_DROP = new Set(["start", "end", "loc", "range", "raw", "regex", "bigint", "sourceType"]);

/** Copy one statement without changing its ESTree structure, adding only
 * scope/literal annotations. A new binder map is used for each statement. */
export function annotateLower(node, meaning, binders = new Map()) {
  if (Array.isArray(node)) return node.map((child) => annotateLower(child, meaning, binders));
  if (node === null || typeof node !== "object") return node;
  const out = {};
  for (const [key, value] of Object.entries(node)) {
    if (!LOWER_DROP.has(key) && !(node.type === "Literal" && key === "value"))
      out[key] = annotateLower(value, meaning, binders);
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
