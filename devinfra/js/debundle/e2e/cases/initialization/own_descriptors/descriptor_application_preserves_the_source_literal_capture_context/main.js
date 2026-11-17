function makeRecord(read) {
  return {
    get value() {
      return read();
    },
  };
}
function wrap(record) {
  const descriptors = Object.getOwnPropertyDescriptors(record);
  const target = {};
  Object.defineProperties(target, descriptors);
  return target;
}
const record = makeRecord(() => later);
const result = wrap(record);
const observed = result.value;
const later = 42;
