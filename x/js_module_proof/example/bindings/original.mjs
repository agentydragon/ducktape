let count = 0;
let amount = 1;
function increment() {
  count += amount;
}
increment();
globalThis.record(count);
export { increment };
