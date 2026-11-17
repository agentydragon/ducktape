function wrap(record) {
  const descriptors = Object.getOwnPropertyDescriptors(record);
  return Object.defineProperties({}, descriptors);
}
const result = wrap({
  get value() {
    return later;
  },
  method() {
    return later;
  },
});
const later = 42;
console.log(result.value, result.method());
