class Holder {
  constructor(record) {
    this.record = record;
  }
  run() {
    return this.record.execute();
  }
}
function create(record) {
  return new Holder(record);
}
const prefix = new globalThis.Date();
const command = create({ execute: () => later });
globalThis.saved = command;
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
