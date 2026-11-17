globalThis.saved = () => later;
Set.prototype.add = function add(value) {
  try {
    globalThis.saved();
  } catch (error) {
    globalThis.observed = error.name;
  }
  return this;
};
const collection = new Set([1]);
const later = 42;
console.log(globalThis.observed, collection.size);
