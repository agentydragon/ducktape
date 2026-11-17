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
  method() {
    return later;
  },
});
const observed = result.value;
const later = 42;
