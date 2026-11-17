var gx = Object.getOwnPropertyDescriptor;
var px = Object.defineProperty;
var dx = (decorators, target, key) => {
  const desc = gx(target, key);
  for (const decorator of decorators) decorator(target, key, desc);
  px(target, key, desc);
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
dx([stamp], Widget.prototype, "name", 1);
console.log(new Widget().name());
export { Widget };
