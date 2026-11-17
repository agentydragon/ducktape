function consume(value) {
  return value.value.get();
}
const descriptors = Object.getOwnPropertyDescriptors({
  get value() {
    return later;
  },
  method() {
    return later;
  },
});
globalThis.saved = descriptors;
consume(globalThis.saved);
const later = 42;
