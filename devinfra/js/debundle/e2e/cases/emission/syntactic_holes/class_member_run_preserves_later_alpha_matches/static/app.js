class Counter {
  helper(seed) {
    return seed + 1;
  }
  run(value) {
    return value * 2;
  }
}
const counter = new Counter();
console.log(counter.run(5));
export { Counter };
