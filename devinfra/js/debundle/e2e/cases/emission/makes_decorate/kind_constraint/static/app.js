var getDescriptor = Object.getOwnPropertyDescriptor;
var defineProperty = Object.defineProperty;
var decorateWidget = (decorators, target, key) => {
  const desc = getDescriptor(target, key);
  for (const decorator of decorators) decorator(target, key, desc);
  defineProperty(target, key, desc);
};
const stamp = (target, key, desc) => {
  const original = desc.value;
  desc.value = function () {
    return original.call(this) + "*";
  };
};
class Widget {
  name() {
    return "w";
  }
}
decorateWidget([stamp], Widget.prototype, "name", 1);
console.log(new Widget().name());
export { Widget };
