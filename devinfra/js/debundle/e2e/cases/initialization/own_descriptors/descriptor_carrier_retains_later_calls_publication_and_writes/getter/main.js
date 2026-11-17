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
Object.defineProperty(descriptors, "extra", {
  get value() {
    return later;
  },
});
const later = 42;
