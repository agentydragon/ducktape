// Compose post-ES2019 expressions into generated syntax using Acorn node
// boundaries. Applying edits from right to left preserves all other source.
export function composeModernExpressions(source, ast, random, marker) {
  const sites = [];
  function visit(node, parent = null, field = null) {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) return node.forEach((child) => visit(child, parent, field));
    if (
      node.type === "Literal" &&
      (node.value === null || typeof node.value === "string" || typeof node.value === "boolean" || (typeof node.value === "number" && Number.isFinite(node.value))) &&
      !node.regex &&
      !(parent?.type === "ExpressionStatement" && parent.directive) &&
      !((parent?.type === "ImportDeclaration" || parent?.type === "ExportNamedDeclaration" || parent?.type === "ExportAllDeclaration") && field === "source") &&
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
      ["nullish", (v) => `(null ?? (${v}))`],
      ["optional", (v) => `(({value: (${v})})?.value)`],
      ["field", (v) => `((new (class { value = (${v}); })()).value)`],
    ];
    const [kind, build] = builders[Math.floor(random() * builders.length)];
    // The VM supplies a collision-free hidden marker. Arguments are evaluated
    // before the marker records that the modern expression completed.
    const text = `${marker}("${kind}", ${build(value)})`;
    return { start: node.start, end: node.end, text: `(${text})` };
  });
  for (const edit of edits.reverse()) source = source.slice(0, edit.start) + edit.text + source.slice(edit.end);
  return { source, sites: edits.length };
}
