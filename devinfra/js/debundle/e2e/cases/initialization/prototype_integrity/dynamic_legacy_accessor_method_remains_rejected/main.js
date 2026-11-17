const methodName = globalThis.methodName || "__defineGetter__";
Object.prototype[methodName]("weight", function () {
  return later;
});
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
const command = create({ execute: () => later });
const later = 42;
console.log(command.run(), command.weight);
