function wrap(record) {
  const descriptors = Object.getOwnPropertyDescriptors(record);
  const target = {};
  Object.defineProperties(target, descriptors);
  return target;
}
const result = wrap({
  set value(next) {
    globalThis.observed = Later;
  },
});
const alias = result;
let observed;
try {
  alias.value = 1;
} catch (error) {
  observed = error.name;
}
class Later {}
console.log(observed);
