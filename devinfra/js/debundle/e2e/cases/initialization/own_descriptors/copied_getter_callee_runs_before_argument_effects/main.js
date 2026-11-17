const items = [];
function wrap(record) {
  const descriptors = Object.getOwnPropertyDescriptors(record);
  const target = {};
  Object.defineProperties(target, descriptors);
  return target;
}
const result = wrap({
  items,
  get run() {
    this.items.push(1);
    return () => 0;
  },
});
let observed;
try {
  result.run(items.forEach(() => Later));
} catch (error) {
  observed = error.name;
}
class Later {}
console.log(observed);
