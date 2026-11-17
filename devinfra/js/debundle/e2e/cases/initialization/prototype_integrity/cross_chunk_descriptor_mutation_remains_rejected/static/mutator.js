export function install(target, getter) {
  Object.defineProperty(target, "weight", { configurable: true, get: getter });
}
