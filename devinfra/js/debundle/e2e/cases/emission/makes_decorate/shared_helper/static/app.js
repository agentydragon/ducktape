var g0 = Object.getOwnPropertyDescriptor;
var p0 = Object.defineProperty;
var d0 = (decorators, target, key) => {
  const desc = g0(target, key);
  for (const decorator of decorators) decorator(target, key, desc);
  p0(target, key, desc);
};
const tag = (target, key, desc) => {
  const original = desc.value;
  desc.value = function () {
    return original.call(this) + "!";
  };
};
class C {
  greet() {
    return "hi";
  }
}
d0([tag], C.prototype, "greet", 1);
console.log(new C().greet());
export { C };
