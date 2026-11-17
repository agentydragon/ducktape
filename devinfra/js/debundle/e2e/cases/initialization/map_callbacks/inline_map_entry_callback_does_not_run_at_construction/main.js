const registry = new Map([["read", () => later]]);
const later = 42;
console.log(registry.get("read")());
export { registry, later };
