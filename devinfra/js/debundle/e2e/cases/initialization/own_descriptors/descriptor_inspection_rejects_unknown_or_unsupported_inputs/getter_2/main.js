const descriptors = Object.getOwnPropertyDescriptors({
  __proto__: null,
  get value() {
    return later;
  },
});

const later = 42;
