function make() {
  const result = {};
  result.observedProperty = 1;
  return result;
}
const item = make();
Object.defineProperty(Object.prototype, "observedProperty", {
  configurable: true,
  set(value) {
    console.log("setter", value);
  },
});
console.log("done");
