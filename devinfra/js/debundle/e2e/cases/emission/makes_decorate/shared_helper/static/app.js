var getDescriptor = Object.getOwnPropertyDescriptor;
var defineProperty = Object.defineProperty;
var decorate = (decorators, target, key) => {
  const desc = getDescriptor(target, key);
  for (const decorator of decorators) decorator(target, key, desc);
  defineProperty(target, key, desc);
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
decorate([tag], C.prototype, "greet", 1);
console.log(new C().greet());
export { C };
