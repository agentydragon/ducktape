async function install() {
  await 0;
  Object.defineProperty(Object.prototype, "weight", {
    configurable: true,
    get() {
      return later;
    },
  });
}
install();
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
Promise.resolve().then(() => console.log(command.record.weight));
const later = 42;
console.log(command.run(), command.weight);
