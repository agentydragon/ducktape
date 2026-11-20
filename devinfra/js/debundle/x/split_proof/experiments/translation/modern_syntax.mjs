// Compose post-ES2019 expressions into generated syntax using Acorn node
// boundaries. Applying edits from right to left preserves all other source.
export function composeModernExpressions(source, ast, random) {
  const sites = [];
  function visit(node, parent = null, field = null) {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) return node.forEach((child) => visit(child, parent, field));
    if (
      node.type === "Literal" &&
      typeof node.value === "number" &&
      Number.isFinite(node.value) &&
      !((parent?.type === "Property" || parent?.type === "MethodDefinition" || parent?.type === "PropertyDefinition") &&
        field === "key" &&
        !parent.computed)
    ) sites.push(node);
    for (const [key, child] of Object.entries(node)) if (child && typeof child === "object") visit(child, node, key);
  }
  visit(ast);
  // Keep growth bounded even for large generated programs.
  const edits = sites.slice(0, 12).map((node) => {
    const value = source.slice(node.start, node.end);
    const builders = [
      (v) => `(null ?? (${v}))`,
      (v) => `(({value: (${v})})?.value)`,
      (v) => `((new (class { value = (${v}); })()).value)`,
    ];
    return { start: node.start, end: node.end, text: builders[Math.floor(random() * builders.length)](value) };
  });
  for (const edit of edits.reverse()) source = source.slice(0, edit.start) + edit.text + source.slice(edit.end);
  return { source, sites: edits.length };
}
