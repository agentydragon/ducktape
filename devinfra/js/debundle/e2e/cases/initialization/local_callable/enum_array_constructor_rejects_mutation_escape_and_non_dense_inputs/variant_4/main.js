var Flags = ((values) => ((values[(values.FIRST = 1)] = "FIRST"), values))(Flags || {});

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
const command = create({ constraints: [, Flags.FIRST], execute: () => later });

const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = dynamic;
