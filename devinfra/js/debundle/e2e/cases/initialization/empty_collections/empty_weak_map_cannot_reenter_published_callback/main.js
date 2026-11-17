globalThis.saved = () => later;
const collection = new WeakMap();
const later = 42;
console.log(collection instanceof WeakMap, later);
