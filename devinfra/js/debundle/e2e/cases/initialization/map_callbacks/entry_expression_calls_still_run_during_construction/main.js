function readKey() {
  return later;
}
function readValue() {
  return later;
}
const registry = new Map([[readKey(), readValue()]]);
const later = 42;
export { readKey, readValue, registry, later };
