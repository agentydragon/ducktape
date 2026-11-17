class Holder {
  constructor(record, policy) {
    this.record = record;
    this.policy = policy;
    this.weight = record.weight ?? 0;
  }
  run() {
    return this.record.execute();
  }
}
class Policy {
  allowed() {
    return true;
  }
}
function create(record) {
  return new Holder(record, new Policy());
}
const prefix = new globalThis.Date();
const command = create({ execute: () => later });
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = 42;
console.log(later);
console.log(command.run(), command.weight);
