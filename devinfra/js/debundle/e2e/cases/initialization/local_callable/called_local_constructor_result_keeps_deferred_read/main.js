let later = 0;
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
const command = create({ execute: () => later });
const early = command.run();
const dynamic = ((object) => {
  object[(object.key = 0)] = "value";
  return 1;
})({});
later = 42;
console.log(early, later);
