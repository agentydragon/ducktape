const Object = {
  getOwnPropertyDescriptors(record) {
    return record.value;
  },
};
const descriptors = Object.getOwnPropertyDescriptors({
  get value() {
    return later;
  },
});

const later = 42;
