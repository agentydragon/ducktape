const descriptors = Object.getOwnPropertyDescriptors({
  get value() {
    return later;
  },
  method() {
    return later;
  },
});
descriptors.extra = () => later;
const target = {};
Object.defineProperties(target, descriptors);
const later = 42;
