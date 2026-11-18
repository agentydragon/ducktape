class Counter {
  constructor() {
    this.value = 0;
  }
  increment() {
    this.value += 1;
    return this.value;
  }
  reset() {
    this.value = 0;
  }
}
const counter = new Counter();
console.log(counter.increment());
export { Counter };
