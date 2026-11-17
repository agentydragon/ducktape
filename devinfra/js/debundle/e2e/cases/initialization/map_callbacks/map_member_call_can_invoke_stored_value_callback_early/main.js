const registry = new Map([["read", () => later]]);
const result = registry.get("read")();
const later = 42;
export { registry, result, later };
