const peek = () => later;
const target = new Proxy(
  {},
  {
    ownKeys() {
      globalThis.observed = peek();
      return [];
    },
  }
);
const early = Object.freeze(target);
var later = "later";
console.log(globalThis.observed, later);
export { early, later };
