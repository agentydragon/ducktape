let record;
const descriptors = Object.getOwnPropertyDescriptors(
  (record = {
    get value() {
      return later;
    },
  })
);
const later = 42;
console.log(descriptors.value.get());
