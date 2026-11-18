class Widget {
  setup() {
    return 0;
  }
  open() {
    return 1;
  }
  tick() {
    return 2;
  }
  close() {
    return 3;
  }
}
console.log(new Widget().open() + new Widget().close());
export { Widget };
