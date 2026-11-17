globalThis.saved = () => later;
const collection = new Map();
const later = 42;
console.log(collection instanceof Map, later);
