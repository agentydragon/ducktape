globalThis.saved = () => later;
Map = function () {
  globalThis.saved();
  return new globalThis.Map();
};
const registry = new Map([["read", () => later]]);
const later = 42;
export { registry, later };
