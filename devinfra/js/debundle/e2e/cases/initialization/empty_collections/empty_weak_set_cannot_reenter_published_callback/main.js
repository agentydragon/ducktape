globalThis.saved = () => later;
const collection = new WeakSet();
const later = 42;
console.log(collection instanceof WeakSet, later);
