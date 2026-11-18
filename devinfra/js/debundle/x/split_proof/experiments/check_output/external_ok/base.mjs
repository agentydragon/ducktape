const PI = 3.14159;
function square(x) {
  return x * x;
}
const area = (r) => PI * square(r);
const registry = [];
function register(name, fn) {
  registry.push(name);
  return fn;
}

export { area, register, registry };
