var ga = Object.getOwnPropertyDescriptor;
var pa = Object.defineProperty;
var da = (decorators, target, key) => {
  const desc = ga(target, key);
  for (const decorator of decorators) decorator(target, key, desc);
  pa(target, key, desc);
};
var gb = Object.getOwnPropertyDescriptor;
var pb = Object.defineProperty;
var db = (decorators, target, key) => {
  const desc = gb(target, key);
  for (const decorator of decorators) decorator(target, key, desc);
  pb(target, key, desc);
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
da([mark("A")], Alpha.prototype, "alphaLabel", 1);
db([mark("B")], Beta.prototype, "betaLabel", 1);
console.log(new Alpha().alphaLabel(), new Beta().betaLabel());
export { Alpha, Beta };
