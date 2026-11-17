var defineAlias = Object.defineProperty,
  descriptorAlias = Object.getOwnPropertyDescriptor,
  decorateHelper = (decorators, target, key, kind) => {
    const desc = kind ? descriptorAlias(target, key) : target;
    for (const decorator of decorators) decorator(target, key, desc);
    defineAlias(target, key, desc);
  };
const tag = (target, key, desc) => {
  const original = desc.value;
  desc.value = function () {
    return original.call(this) + "!";
  };
};
const firstValue = 100,
  targetClass = class LocalWidget {
    greet() {
      return "hi";
    }
  };
decorateHelper([tag], targetClass.prototype, "greet", 1);
console.log(new targetClass().greet(), firstValue);
export { targetClass };
