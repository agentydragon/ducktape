const descriptors = Object.getOwnPropertyDescriptors({
  get value() {
    return later;
  },
  method() {
    return later;
  },
});
const later = 42;
console.log(descriptors.value.get(), descriptors.method.value());
