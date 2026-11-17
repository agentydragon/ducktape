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
const later = 42;
console.log(descriptors.value.get(), descriptors.method.value());
