var alphaGetDescriptor = Object.getOwnPropertyDescriptor;
var alphaDefineProperty = Object.defineProperty;
var decorateAlpha = (decorators, target, key) => {
  const desc = alphaGetDescriptor(target, key);
  for (const decorator of decorators) decorator(target, key, desc);
  alphaDefineProperty(target, key, desc);
};
var betaGetDescriptor = Object.getOwnPropertyDescriptor;
var betaDefineProperty = Object.defineProperty;
var decorateBeta = (decorators, target, key) => {
  const desc = betaGetDescriptor(target, key);
  for (const decorator of decorators) decorator(target, key, desc);
  betaDefineProperty(target, key, desc);
};
const mark = (suffix) => (target, key, desc) => {
  const original = desc.value;
  desc.value = function () {
    return original.call(this) + suffix;
  };
};
class Alpha {
  alphaLabel() {
    return "a";
  }
}
class Beta {
  betaLabel() {
    return "b";
  }
}
decorateAlpha([mark("A")], Alpha.prototype, "alphaLabel", 1);
decorateBeta([mark("B")], Beta.prototype, "betaLabel", 1);
console.log(new Alpha().alphaLabel(), new Beta().betaLabel());
export { Alpha, Beta };
