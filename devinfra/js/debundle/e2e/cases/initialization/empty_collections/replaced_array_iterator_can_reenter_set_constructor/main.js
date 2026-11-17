globalThis.saved = () => later;
Array.prototype[Symbol.iterator] = function* () {
  try {
    globalThis.saved();
  } catch (error) {
    globalThis.observed = error.name;
  }
  yield 1;
};
const collection = new Set([1]);
const later = 42;
console.log(globalThis.observed, collection.size);
