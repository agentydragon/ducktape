const cache = new WeakMap(),
  limit = 10;
console.log(cache instanceof WeakMap, limit);
export { cache, limit };
