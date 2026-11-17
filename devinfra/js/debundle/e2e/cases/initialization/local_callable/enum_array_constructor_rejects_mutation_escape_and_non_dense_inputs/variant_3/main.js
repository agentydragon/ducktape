var Flags = ((values) => ((values[(values.FIRST = 1)] = "FIRST"), values))(Flags || {});
Object.defineProperty(Object.prototype, "MISSING", {
  get() {
    return 1;
  },
});
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
const command = create({ constraints: [Flags.MISSING], execute: () => later });

const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
