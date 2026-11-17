globalThis.saved = () => later;
const items = {
  [Symbol.iterator]() {
    try {
      globalThis.saved();
    } catch (error) {
      globalThis.observed = error.name;
    }
    return [][Symbol.iterator]();
  },
};
const collection = new Map(items);
const later = 42;
console.log(globalThis.observed, collection.size);
