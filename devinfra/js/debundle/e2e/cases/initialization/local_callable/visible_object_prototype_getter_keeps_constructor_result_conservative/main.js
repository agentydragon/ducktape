let later = 0;
Object.defineProperty(Object.prototype, "weight", {
  get() {
    return later;
  },
  configurable: true,
});
class Holder {
  constructor(record) {
    this.record = record;
    this.weight = record.weight ?? 0;
  }
  run() {
    return this.record.execute();
  }
}
function create(record) {
  return new Holder(record);
}
const command = create({ execute: () => later });
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
later = 42;
