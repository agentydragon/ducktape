globalThis.saved = () => later;
function Map() {
  globalThis.saved();
  return new globalThis.Map();
}
const registry = new Map([["read", () => later]]);
const later = 42;
export { Map, registry, later };
