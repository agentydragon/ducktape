globalThis.saved = () => later;
globalThis.Set = function Set() {
  try {
    globalThis.saved();
    return { value: "ready" };
  } catch (error) {
    return { value: error.name };
  }
};
const collection = new Set();
const later = 42;
console.log(collection.value);
