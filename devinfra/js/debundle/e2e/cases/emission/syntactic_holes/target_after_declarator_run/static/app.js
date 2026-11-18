const limit = 10,
  cache = new WeakMap();
function lookup(n) {
  return cache.get(n) ?? limit;
}
console.log(lookup({}));
export { lookup };
