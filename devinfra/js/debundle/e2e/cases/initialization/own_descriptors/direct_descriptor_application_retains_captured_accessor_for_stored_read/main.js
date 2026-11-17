function wrap(record) {
  const descriptors = Object.getOwnPropertyDescriptors(record);
  return Object.defineProperties({}, descriptors);
}
const result = wrap({
  get value() {
    return Later;
  },
});
let touched;
let observed;
try {
  touched = result.value;
} catch (error) {
  observed = error.name;
}
class Later {}
console.log(observed);
