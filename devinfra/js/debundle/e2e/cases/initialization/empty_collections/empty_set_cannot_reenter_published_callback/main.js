globalThis.saved = () => later;
const collection = new Set();
const later = 42;
console.log(collection instanceof Set, later);
