const limit = 10,
  cache = new WeakMap();
function lookup(n, e) {
  let t = cache.get(n);
  if (!t) {
    t = { [e]: limit };
    cache.set(n, t);
  }
  return t[e];
}
console.log(lookup({}, "x"));
export { lookup };
