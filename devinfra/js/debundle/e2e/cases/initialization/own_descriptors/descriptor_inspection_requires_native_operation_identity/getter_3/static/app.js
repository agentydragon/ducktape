import "./replacement.js";
const descriptors = Object.getOwnPropertyDescriptors({
  get value() {
    return later;
  },
});
const later = 42;
