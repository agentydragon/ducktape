const registry = new Map([[() => later, "value"]]);
const result = opaqueConsumer(registry);
const later = 42;
export { registry, result, later };
