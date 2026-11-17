const registry = new Map([["read", () => later]]);
const result = opaqueConsumer(registry);
const later = 42;
export { registry, result, later };
