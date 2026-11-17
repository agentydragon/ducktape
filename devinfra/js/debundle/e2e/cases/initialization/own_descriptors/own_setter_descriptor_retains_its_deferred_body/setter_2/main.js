const descriptors = Object.getOwnPropertyDescriptors({
  set value(next) {
    globalThis.observed = next + later;
  },
});
descriptors.value.set(8);
const later = 42;
