globalThis.saved = () => later;
const items = {
  [Symbol.iterator]() {
    globalThis.saved();
    return [][Symbol.iterator]();
  },
};
const registry = new Map(items);
const later = 42;
export { items, registry, later };
