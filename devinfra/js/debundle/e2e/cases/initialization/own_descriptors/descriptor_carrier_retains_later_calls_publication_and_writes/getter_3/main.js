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
descriptors.method.value();
const later = 42;
