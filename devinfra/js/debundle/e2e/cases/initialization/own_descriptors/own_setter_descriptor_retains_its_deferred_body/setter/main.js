const descriptors = Object.getOwnPropertyDescriptors({
  set value(next) {
    globalThis.observed = next + later;
  },
});
const later = 42;
descriptors.value.set(8);
console.log(globalThis.observed);
