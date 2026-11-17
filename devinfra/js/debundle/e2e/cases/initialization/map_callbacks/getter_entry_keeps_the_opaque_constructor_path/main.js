globalThis.saved = () => later;
const registry = new Map([
  {
    get 0() {
      globalThis.saved();
      return "read";
    },
    1: "value",
  },
]);
const later = 42;
export { registry, later };
