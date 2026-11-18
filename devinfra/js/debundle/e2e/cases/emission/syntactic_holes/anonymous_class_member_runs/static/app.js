class RuntimeCounter {
  constructor(start) {
    this.value = start;
  }
  increment() {
    this.value += 1;
  }
  label() {
    return `count:${this.value}`;
  }
  reset() {
    this.value = 0;
  }
}
const counter = new RuntimeCounter(4);
counter.increment();
console.log(counter.label());
export { RuntimeCounter };
