var Flags = ((values) => ((values[(values.FIRST = 1)] = "FIRST"), (values[(values.SECOND = 2)] = "SECOND"), values))(
  Flags || {}
);
class Holder {
  constructor(record, policy, callback) {
    this.record = record;
    this.policy = policy;
    this.callback = callback;
    this.weight = record.weight ?? 0;
  }
  run() {
    return this.record.execute() + this.callback();
  }
}
class Policy {
  allowed() {
    return true;
  }
}
function create(record) {
  return new Holder(record, new Policy(), () => later);
}
const prefix = new globalThis.Date();
const command = create({ constraints: [Flags.FIRST, Flags.SECOND], execute: () => later });
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
const later = 42;
console.log(later);
console.log(command.run(), command.weight, command.record.constraints.length);
