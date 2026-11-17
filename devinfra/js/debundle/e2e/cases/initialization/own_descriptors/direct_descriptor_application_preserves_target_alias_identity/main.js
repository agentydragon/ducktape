const descriptors = Object.getOwnPropertyDescriptors({
  get value() {
    return later;
  },
  method() {
    return later;
  },
});
const target = {};
const result = Object.defineProperties(target, descriptors);
const alias = result;
const later = 42;
console.log(result === target, alias === target, alias.value, alias.method());
