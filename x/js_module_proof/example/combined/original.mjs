let count = 0;
function increment() {
  count += 1;
}
increment();
globalThis.record(count);
function current() {
  return count;
}
export { current };
