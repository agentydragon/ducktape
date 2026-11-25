function make() {
  const result = {};
  result.observedProperty = 1;
  return result;
}
Object.defineProperty(Object.prototype, "observedProperty", {
  configurable: true,
  set(value) {
    console.log("setter", value);
  },
});
const item = make();
console.log("done");
