const items = [];
function wrap(record) {
  const descriptors = Object.getOwnPropertyDescriptors(record);
  const target = {};
  Object.defineProperties(target, descriptors);
  return target;
}
const result = wrap({
  items,
  get value() {
    this.items.push(1);
    return 0;
  },
});
const touched = result.value;
let observed;
try {
  items.forEach(() => Later);
} catch (error) {
  observed = error.name;
}
class Later {}
console.log(touched, observed);
