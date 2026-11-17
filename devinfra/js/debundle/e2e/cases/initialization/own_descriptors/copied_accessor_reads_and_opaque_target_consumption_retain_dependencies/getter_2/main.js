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
result.value();
const later = 42;
