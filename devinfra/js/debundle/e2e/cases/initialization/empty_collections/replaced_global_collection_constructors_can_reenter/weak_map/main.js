globalThis.saved = () => later;
const key = "WeakMap";
globalThis[key] = function WeakMap() {
  try {
    globalThis.saved();
    return { value: "ready" };
  } catch (error) {
    return { value: error.name };
  }
};
const collection = new WeakMap();
const later = 42;
console.log(collection.value);
