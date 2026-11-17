export function observe(callback) {
  return (...args) => callback(...args);
}
