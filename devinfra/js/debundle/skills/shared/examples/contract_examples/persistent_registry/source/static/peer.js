const callbacks = new Set();
export function register(callback) {
  callbacks.add(callback);
}
export function emit() {
  return [...callbacks].map((callback) => callback());
}
