function makeRegistry() {
  eval("1");
  return new Map([["read", () => later]]);
}
const registry = makeRegistry();
const later = 42;
export { makeRegistry, registry, later };
