function inspect(callback) {
  return Object.getOwnPropertyDescriptors({
    get value() {
      return callback();
    },
    method() {
      return callback();
    },
  });
}
const descriptors = inspect(() => later);
const before = descriptors.method.value();
const later = 42;
