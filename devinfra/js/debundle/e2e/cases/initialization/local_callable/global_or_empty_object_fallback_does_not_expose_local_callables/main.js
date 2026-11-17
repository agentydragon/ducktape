const prefix = (() => {
  const runtime = typeof window !== "undefined" ? window : globalThis || {};
  const entry = new runtime.Error();
  runtime.registry = runtime.registry || {};
  runtime.registry[entry.name] = entry.message;
  return undefined;
})();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
function readLater() {
  return later;
}
console.log(later);
