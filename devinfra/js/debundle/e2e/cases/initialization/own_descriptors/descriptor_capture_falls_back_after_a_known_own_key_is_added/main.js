const record = {
  method() {
    return later;
  },
};
[0].forEach(() => {
  record.extra = 1;
});
const descriptors = Object.getOwnPropertyDescriptors(record);
const target = {};
Object.defineProperties(target, descriptors);
const later = 42;
