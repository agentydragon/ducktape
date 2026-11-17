function wrap(record) {
  const descriptors = Object.getOwnPropertyDescriptors(record);
  const target = {};
  Object.defineProperties(target, descriptors);
  return target;
}
const result = wrap({
  get value() {
    return later;
  },
});
const key = globalThis.key;
const observed = result[key];
const later = 42;
