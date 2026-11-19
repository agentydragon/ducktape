const PI = 3.14159;
function square(x) {
  return x * x;
}
const area = (r) => PI * square(r);

function isEven(n) {
  return n === 0 || isOdd(n - 1);
}
function isOdd(n) {
  return n !== 0 && isEven(n - 1);
}

const registry = [];
function register(name, fn) {
  registry.push(name);
  return fn;
}
register("area", area);

const label = (s) => "<" + s + ">";
register("label", label);

console.log(label("circle"), area(2), isEven(10));
console.log(registry.join(","));
