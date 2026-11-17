globalThis.saved = () => later;
const root = globalThis;
root.Map = function Map() {
  try {
    globalThis.saved();
    return { value: "ready" };
  } catch (error) {
    return { value: error.name };
  }
};
const collection = new Map();
const later = 42;
console.log(collection.value);
