function inspect(options) {
  return options.missing;
}
Object.defineProperty(Object.prototype, "missing", {
  configurable: true,
  get() {
    console.log("getter");
  },
});
const result = inspect({});
console.log("done");
