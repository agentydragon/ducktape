function inspect(options) {
  return options.missing;
}
const result = inspect({});
Object.defineProperty(Object.prototype, "missing", {
  configurable: true,
  get() {
    console.log("getter");
  },
});
console.log("done");
