const selectedStore = class {
  status = "idle";
  run() {
    return base(this);
  }
  describe() {
    return tag("unique-store-token");
  }
};
export { selectedStore };
