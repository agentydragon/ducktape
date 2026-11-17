globalThis.saved = () => later;
globalThis.WeakSet = function WeakSet() {
  try {
    globalThis.saved();
    return { value: "ready" };
  } catch (error) {
    return { value: error.name };
  }
};
const collection = new WeakSet();
const later = 42;
console.log(collection.value);
